"""Wiring tests for the option-writer DEFAULTS Rust-render seam (roadmap PR-2).

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1891) — the
``_atomic_write_parquet`` seam routed through the COMMON
``lake_rust_render`` facade under :data:`PROFILE_OPTIONS_DEFAULTS`.
Crate-FREE by design: fake ``brutex_lake`` modules (the faithful one
implemented ON TOP of the installed Python polars, so its output is
genuinely byte-comparable) drive the flag-ON routing through the REAL
writer/heal entries; the flag-OFF arm is pinned mechanism-verbatim with a
never-routes spy. Real-crate byte parity lives in
``tests/golden/test_option_lake_rust_render_parity.py`` (the parity lane).

Carries the PR-2 rails-design regressions R1/R2/R4–R9/R12/R13 plus the P1
standing pin (``df.write_parquet(BytesIO)`` ≡ ``df.write_parquet(path)`` —
the equivalence that makes ``_defaults_python_render`` byte-representative
of what flag-OFF actually streams to disk; judge-probed PASS 7/7 on the
2026-07-11 corpus, pinned here so it can never silently drift).

Whitebox (DEC-P0-017): drives the private ``_atomic_write_parquet`` /
``_defaults_python_render`` / ``_render_df`` seams directly, so
``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import io
import sys
import types
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import polars as pl
import pytest
from brutex.data import lake_rust_render as lrr
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import (
    OptionHealResult,
    OptionParquetWriter,
    OptionSeriesCoordinate,
    OptionWriterError,
    _defaults_python_render,
    _render_df,
)
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _strict_off_degrade_lattice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degrade-lattice suite — pin ``BRUTEX_RUST_STRICT=0``.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships strict mode ON by default, so a
    crate that is absent / stale / errored / burn-in-poisoned now RAISES
    ``RustFallbackRefused`` at the facade choke point instead of quietly
    serving the Python oracle.  The tests below exist precisely to exercise
    that oracle machinery — the still-supported ``BRUTEX_RUST_STRICT=0``
    mode — so the mode is DECLARED once, here, rather than by weakening any
    assertion.  Strict-ON refusal is pinned in each facade's own
    ``TestStrictRefusal`` class.

    Uses the SAME shared ``monkeypatch`` object as the sibling autouse
    fixtures (never its own ``MonkeyPatch.context()``): one object means one
    LIFO undo stack, so the value cannot leak past this module.
    """
    from brutex.config import rust_strict

    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrr.reset_lake_rust_render_for_tests()
    yield
    lrr.reset_lake_rust_render_for_tests()


_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"

_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
)


def _bar(*, side: str = "CE", minute: int = 0, oi: int | None = 1200) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.25"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250 + minute,
        open_interest=oi,
        side=side,
        strike=None if oi is None else Decimal("17500.00"),
        spot=None if oi is None else Decimal("17480.57"),
        iv=None if oi is None else Decimal("0.1523"),
    )


def _writer(lake: Path) -> OptionParquetWriter:
    return OptionParquetWriter(lake_root=lake)


def _write(lake: Path, bars: list[OptionBar]) -> int:
    return _writer(lake).write(partition_key=_PK, coordinate=_COORD, bars=bars)


def _dest(lake: Path) -> Path:
    return lake / "options" / _SERIES / "2021-01.parquet"


def _poison_with_dup(dest: Path) -> bytes:
    """Append a value-drifted duplicate key to the stored file (heal-suite idiom).

    Returns the poisoned bytes (the expected quarantine before-image).
    """
    df = pl.read_parquet(dest, glob=False)
    drifted = df[0:1].with_columns((pl.col("close") + 999.0).alias("close"))
    pl.concat([df, drifted]).write_parquet(dest)
    return dest.read_bytes()


def _fake_crate(
    *,
    defaults_render: Callable[[bytes], object] | None = None,
    pinned: str | None = None,
) -> types.ModuleType:
    """A fake ``brutex_lake`` carrying ALL FOUR ``_CRATE_API`` callables.

    The FAITHFUL default ``defaults_render`` re-renders the IPC handoff with
    the installed wheel's zero-kwarg ``write_parquet`` — byte-identical to
    ``_defaults_python_render`` by construction, so the burn-in compare
    passes and the ROUTED path is proven end-to-end without the compiled
    crate. The bars callable raises: this suite drives the OPTIONS profile
    exclusively, so any bars-profile routing is a wiring bug.
    """
    module = types.ModuleType("brutex_lake")

    def _faithful(ipc_bytes: bytes) -> bytes:
        df = pl.read_ipc(io.BytesIO(ipc_bytes))
        bio = io.BytesIO()
        df.write_parquet(bio)
        return bio.getvalue()

    def _bars_must_not_run(ipc_bytes: bytes) -> bytes:
        raise AssertionError("the option writer must route the OPTIONS profile, not bars")

    module.render_parquet_from_ipc = _bars_must_not_run  # type: ignore[attr-defined]
    module.render_parquet_defaults_from_ipc = (  # type: ignore[attr-defined]
        defaults_render if defaults_render is not None else _faithful
    )
    module.pinned_polars_version = lambda: (  # type: ignore[attr-defined]
        pl.__version__ if pinned is None else pinned
    )
    module.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]
    return module


def _arm(
    monkeypatch: pytest.MonkeyPatch,
    crate: types.ModuleType,
    *,
    remaining: int = lrr.BURN_IN_SAMPLES,
) -> None:
    """Install *crate* + set the flag ON + reset the facade view."""
    monkeypatch.setitem(sys.modules, "brutex_lake", crate)
    monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
    lrr.reset_lake_rust_render_for_tests(remaining=remaining)


class _RenderSpy:
    """Counting wrapper around the fake crate's defaults render."""

    def __init__(self, inner: Callable[[bytes], object] | None = None) -> None:
        self.calls = 0
        self._inner = inner

    def __call__(self, ipc_bytes: bytes) -> object:
        self.calls += 1
        if self._inner is None:
            df = pl.read_ipc(io.BytesIO(ipc_bytes))
            bio = io.BytesIO()
            df.write_parquet(bio)
            return bio.getvalue()
        return self._inner(ipc_bytes)


class _IpcSpy:
    """Counting wrapper around the COMMON IPC handoff."""

    def __init__(self) -> None:
        self.calls = 0
        self._real = lrr.ipc_handoff_bytes

    def __call__(self, df: pl.DataFrame) -> bytes:
        self.calls += 1
        return self._real(df)


# ---------------------------------------------------------------------------
# R12 — the never-routes spy (flag-OFF is mechanism-verbatim)
# ---------------------------------------------------------------------------


class TestFlagOffNeverRoutes:
    def test_flag_off_write_and_heal_never_touch_the_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R12: flag "0" (the kill switch — the default flipped ON under
        DEC-RUST-DEFAULT-ON-WAVE-001) ⇒ the facade's routed entry is NEVER invoked from
        any option-writer path, and the stored bytes equal the pre-PR-2
        output (the verbatim legacy union expression rendered inline)."""
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")

        def _must_not_route(**_: object) -> bytes:
            raise AssertionError("flag-OFF must never call render_parquet_bytes")

        monkeypatch.setattr(lrr, "render_parquet_bytes", _must_not_route)
        lake = tmp_path / "lake"
        bars = [_bar(minute=0), _bar(minute=1), _bar(side="PE", minute=0)]
        assert _write(lake, bars) == 3
        dest = _dest(lake)
        # Pre-PR-2 byte pin: the documented union expression, rendered with
        # the verbatim legacy call shape (write_parquet straight to a path).
        expected = (
            _render_df(_COORD, bars)
            .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
            .sort(["side", "timestamp"])
        )
        scratch = tmp_path / "expected.parquet"
        expected.write_parquet(scratch)
        assert dest.read_bytes() == scratch.read_bytes()
        # The HEAL rewrite is flag-OFF verbatim too.
        _poison_with_dup(dest)
        result = _writer(lake).heal_partition(dest, quarantine_to=tmp_path / "q" / "b.parquet")
        assert result.changed is True
        assert dest.read_bytes() == scratch.read_bytes()

    @pytest.mark.parametrize("raw", ["0", "false", "off"])
    def test_falsy_flag_never_routes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, raw)

        def _must_not_route(**_: object) -> bytes:
            raise AssertionError("a falsy flag must never route")

        monkeypatch.setattr(lrr, "render_parquet_bytes", _must_not_route)
        assert _write(tmp_path / "lake", [_bar()]) == 1


# ---------------------------------------------------------------------------
# The P1 standing pin — BytesIO oracle ≡ path-streamed flag-OFF bytes
# ---------------------------------------------------------------------------


class TestP1BytesIoEqualsPathTarget:
    """``_defaults_python_render`` (BytesIO) must stay byte-representative of
    the flag-OFF ``df.write_parquet(<tmp path>)`` stream — the wheel routes
    both targets into the SAME streaming sink (judge-probed PASS 7/7)."""

    @pytest.mark.parametrize(
        "bars",
        [
            pytest.param([_bar(minute=i) for i in range(8)], id="ce-only"),
            pytest.param(
                [
                    _bar(side=("CE" if i % 2 else "PE"), minute=i, oi=(None if i % 3 else 10 + i))
                    for i in range(20)
                ],
                id="mixed-sparse-nulls",
            ),
            pytest.param([_bar(minute=i, oi=None) for i in range(6)], id="all-null-nullables"),
        ],
    )
    def test_bytesio_equals_path_bytes(self, tmp_path: Path, bars: list[OptionBar]) -> None:
        frame = (
            _render_df(_COORD, bars)
            .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
            .sort(["side", "timestamp"])
        )
        path_target = tmp_path / "path-target.parquet"
        frame.write_parquet(path_target)
        assert _defaults_python_render(frame) == path_target.read_bytes()

    def test_null_side_heal_frame_bytesio_equals_path(self, tmp_path: Path) -> None:
        """The hand-planted null-side heal shape (Utf8 null_count surface)."""
        frame = _render_df(_COORD, [_bar(minute=0), _bar(side="PE", minute=1)])
        null_side = frame[0:1].with_columns(
            pl.lit(None, dtype=pl.String).alias("side"),
            (pl.col("timestamp") + pl.duration(minutes=99)).alias("timestamp"),
        )
        heal_frame = (
            pl.concat([frame, null_side])
            .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
            .sort(["side", "timestamp"])
        )
        path_target = tmp_path / "path-target.parquet"
        heal_frame.write_parquet(path_target)
        assert _defaults_python_render(heal_frame) == path_target.read_bytes()


# ---------------------------------------------------------------------------
# flag-ON routing — profile wiring + engaged parity (faithful fake crate)
# ---------------------------------------------------------------------------


class TestFlagOnRouting:
    def test_engaged_write_produces_flag_off_identical_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = [_bar(minute=i) for i in range(5)]
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # the kill switch → Python oracle
        off_lake = tmp_path / "off"
        assert _write(off_lake, bars) == 5

        _arm(monkeypatch, _fake_crate())
        on_lake = tmp_path / "on"
        with capture_logs() as logs:
            assert _write(on_lake, bars) == 5
        assert _dest(on_lake).read_bytes() == _dest(off_lake).read_bytes()
        assert not lrr.rust_render_disabled()  # the burn-in compare passed
        engaged = [e for e in logs if e["event"] == "lake_rust_render.engaged"]
        assert engaged and engaged[0]["profile"] == lrr.PROFILE_OPTIONS_DEFAULTS

    def test_call_shape_r13_no_path_context_and_options_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R13: the facade call passes NO path context (the series dir name
        is vendor-influenceable — the log-injection discipline) and routes
        the OPTIONS profile with the frame's schema fingerprint."""
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> bytes:
            captured.update(kwargs)
            render = cast("Callable[[], bytes]", kwargs["python_render"])
            return render()

        monkeypatch.setattr(lrr, "render_parquet_bytes", _capture)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        assert _write(tmp_path / "lake", [_bar()]) == 1
        assert set(captured) == {
            "ipc_bytes_factory",
            "python_render",
            "polars_version",
            "schema_token",
            "profile",
        }
        assert captured["profile"] == lrr.PROFILE_OPTIONS_DEFAULTS
        assert captured["polars_version"] == pl.__version__
        token = captured["schema_token"]
        assert isinstance(token, str)
        assert token.startswith("Schema(")  # the frame schema fingerprint
        assert "underlying_symbol" in token  # column names, never a dest path
        assert _SERIES not in token and str(tmp_path) not in token


# ---------------------------------------------------------------------------
# R1 — no-op paths never pay a render
# ---------------------------------------------------------------------------


class TestR1NoopPathsNeverRender:
    def test_noop_heal_zero_crate_zero_ipc_byte_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        assert _write(lake, [_bar(minute=0), _bar(minute=1)]) == 2
        dest = _dest(lake)
        before = dest.read_bytes()
        spy = _RenderSpy()
        ipc_spy = _IpcSpy()
        _arm(monkeypatch, _fake_crate(defaults_render=spy))
        monkeypatch.setattr(lrr, "ipc_handoff_bytes", ipc_spy)
        result = _writer(lake).heal_partition(dest)  # clean file — nothing to heal
        assert result.changed is False
        assert dest.read_bytes() == before
        assert spy.calls == 0
        assert ipc_spy.calls == 0

    def test_pure_repull_merge_zero_crate_zero_ipc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        bars = [_bar(minute=0), _bar(minute=1)]
        assert _write(lake, bars) == 2
        before = _dest(lake).read_bytes()
        spy = _RenderSpy()
        ipc_spy = _IpcSpy()
        _arm(monkeypatch, _fake_crate(defaults_render=spy))
        monkeypatch.setattr(lrr, "ipc_handoff_bytes", ipc_spy)
        assert _write(lake, bars) == 0  # pure re-pull — writes nothing
        assert _dest(lake).read_bytes() == before
        assert spy.calls == 0
        assert ipc_spy.calls == 0


# ---------------------------------------------------------------------------
# R2 — quarantine before-image stays byte-verbatim; ONE crate render
# ---------------------------------------------------------------------------


class TestR2QuarantineVerbatim:
    def test_changing_heal_quarantine_is_verbatim_and_crate_renders_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        assert _write(lake, [_bar(minute=0), _bar(side="PE", minute=0)]) == 2
        dest = _dest(lake)
        poisoned = _poison_with_dup(dest)
        spy = _RenderSpy()
        _arm(monkeypatch, _fake_crate(defaults_render=spy))
        quarantine_to = tmp_path / "q" / "2021-01.parquet"
        result = _writer(lake).heal_partition(dest, quarantine_to=quarantine_to)
        assert result.changed is True
        assert quarantine_to.read_bytes() == poisoned  # byte-VERBATIM before-image
        assert spy.calls == 1  # the dest rewrite ONLY — never the quarantine copy


# ---------------------------------------------------------------------------
# R4 — crate errors degrade to oracle bytes; heal semantics unperturbed
# ---------------------------------------------------------------------------


def _heal_result_fields(result: OptionHealResult) -> tuple[object, ...]:
    return (
        result.changed,
        result.kept,
        result.dropped_duplicates,
        result.dropped_removed,
    )


class TestR4CrateErrorsDegrade:
    def test_runtime_error_mid_heal_completes_on_oracle_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Flag-OFF twin first: the expected healed bytes + result.
        off_lake = tmp_path / "off"
        assert _write(off_lake, [_bar(minute=0), _bar(side="PE", minute=0)]) == 2
        _poison_with_dup(_dest(off_lake))
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # the kill switch → Python oracle
        off_result = _writer(off_lake).heal_partition(
            _dest(off_lake), quarantine_to=tmp_path / "qoff" / "b.parquet"
        )

        on_lake = tmp_path / "on"
        assert _write(on_lake, [_bar(minute=0), _bar(side="PE", minute=0)]) == 2
        _poison_with_dup(_dest(on_lake))

        def _boom(ipc_bytes: bytes) -> bytes:
            raise RuntimeError("brutex-lake parquet render panicked (caught at the FFI boundary)")

        _arm(monkeypatch, _fake_crate(defaults_render=_boom))
        with capture_logs() as logs:
            on_result = _writer(on_lake).heal_partition(
                _dest(on_lake), quarantine_to=tmp_path / "qon" / "b.parquet"
            )
        assert _heal_result_fields(on_result) == _heal_result_fields(off_result)
        assert _dest(on_lake).read_bytes() == _dest(off_lake).read_bytes()
        warns = [e for e in logs if e["event"] == "lake_rust_render.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "render_error"
        assert not lrr.rust_render_disabled()  # degrade, never a latch

    def test_panic_exception_shaped_raise_latches_crate_panic_and_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars.exceptions as pl_exc

        lake = tmp_path / "lake"
        assert _write(lake, [_bar(minute=0)]) == 1
        dest = _dest(lake)
        _poison_with_dup(dest)

        def _panic(ipc_bytes: bytes) -> bytes:
            raise pl_exc.PanicException("simulated escaped panic")

        _arm(monkeypatch, _fake_crate(defaults_render=_panic))
        with capture_logs() as logs:
            result = _writer(lake).heal_partition(dest, quarantine_to=tmp_path / "q" / "b.parquet")
        assert result.changed is True  # the heal completed on oracle bytes
        assert lrr.rust_render_disabled()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert disabled and disabled[0]["reason"] == "crate_panic"


# ---------------------------------------------------------------------------
# R5 — MemoryError escapes as LakeWriteHalt (never a degrade)
# ---------------------------------------------------------------------------


class TestR5OomMapsToLakeWriteHalt:
    def test_crate_oom_in_heal_escapes_as_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        assert _write(lake, [_bar(minute=0)]) == 1
        dest = _dest(lake)
        _poison_with_dup(dest)

        def _oom(ipc_bytes: bytes) -> bytes:
            raise MemoryError

        _arm(monkeypatch, _fake_crate(defaults_render=_oom))
        with pytest.raises(LakeWriteHalt) as excinfo:
            _writer(lake).heal_partition(dest, quarantine_to=tmp_path / "q" / "b.parquet")
        assert excinfo.value.reason == "out_of_memory"

    def test_crate_oom_in_merge_escapes_as_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _oom(ipc_bytes: bytes) -> bytes:
            raise MemoryError

        _arm(monkeypatch, _fake_crate(defaults_render=_oom))
        with pytest.raises(LakeWriteHalt) as excinfo:
            _write(tmp_path / "lake", [_bar()])
        assert excinfo.value.reason == "out_of_memory"

    def test_ipc_factory_oom_escapes_as_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate())

        def _oom_ipc(df: pl.DataFrame) -> bytes:
            raise MemoryError

        monkeypatch.setattr(lrr, "ipc_handoff_bytes", _oom_ipc)
        with pytest.raises(LakeWriteHalt) as excinfo:
            _write(tmp_path / "lake", [_bar()])
        assert excinfo.value.reason == "out_of_memory"


# ---------------------------------------------------------------------------
# R6 — version-mismatch crate serves flag-OFF-identical bytes
# ---------------------------------------------------------------------------


class TestR6VersionMismatch:
    def test_mismatched_crate_write_and_heal_serve_flag_off_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = [_bar(minute=0), _bar(side="PE", minute=0)]
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # the kill switch → Python oracle
        off_lake = tmp_path / "off"
        assert _write(off_lake, bars) == 2

        _arm(monkeypatch, _fake_crate(pinned="0.0.0-mismatch"))
        on_lake = tmp_path / "on"
        with capture_logs() as logs:
            assert _write(on_lake, bars) == 2
        assert _dest(on_lake).read_bytes() == _dest(off_lake).read_bytes()
        disabled = [e for e in logs if e["event"] == "lake_rust_render.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "polars_version_mismatch"


# ---------------------------------------------------------------------------
# R7 — burn-in mismatch ships oracle bytes; idempotence oracle holds
# ---------------------------------------------------------------------------


class TestR7BurnInMismatchShipsOracle:
    def test_mismatching_crate_heal_ships_python_bytes_and_stays_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Flag-OFF twin for the expected healed bytes.
        off_lake = tmp_path / "off"
        assert _write(off_lake, [_bar(minute=0), _bar(side="PE", minute=0)]) == 2
        _poison_with_dup(_dest(off_lake))
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # the kill switch → Python oracle
        _writer(off_lake).heal_partition(
            _dest(off_lake), quarantine_to=tmp_path / "qoff" / "b.parquet"
        )

        on_lake = tmp_path / "on"
        assert _write(on_lake, [_bar(minute=0), _bar(side="PE", minute=0)]) == 2
        # Poison BOTH lakes identically AFTER the writes so the heal frames match.
        _poison_with_dup(_dest(on_lake))
        _arm(monkeypatch, _fake_crate(defaults_render=lambda ipc: b"WRONG-crate-bytes"))
        with capture_logs() as logs:
            result = _writer(on_lake).heal_partition(
                _dest(on_lake), quarantine_to=tmp_path / "qon" / "b.parquet"
            )
        assert result.changed is True
        # The ORACLE bytes shipped — the healed file equals the flag-OFF twin.
        assert _dest(on_lake).read_bytes() == _dest(off_lake).read_bytes()
        assert lrr.rust_render_disabled()  # autoreverted for the process
        errors = [e for e in logs if e["event"] == "lake_rust_render.render_autoreverted"]
        assert errors and errors[0]["profile"] == lrr.PROFILE_OPTIONS_DEFAULTS
        # Idempotence oracle: a second heal is a byte-untouched no-op.
        healed = _dest(on_lake).read_bytes()
        second = _writer(on_lake).heal_partition(_dest(on_lake))
        assert second.changed is False
        assert _dest(on_lake).read_bytes() == healed


# ---------------------------------------------------------------------------
# R8 — containment refusal precedes any render
# ---------------------------------------------------------------------------


class TestR8ContainmentRefusalPrecedesRender:
    def test_symlinked_dest_heal_refused_with_zero_crate_and_ipc_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        assert _write(lake, [_bar(minute=0)]) == 1
        real = _dest(lake)
        swapped = real.parent / "2021-02.parquet"
        swapped.symlink_to(real)
        spy = _RenderSpy()
        ipc_spy = _IpcSpy()
        _arm(monkeypatch, _fake_crate(defaults_render=spy))
        monkeypatch.setattr(lrr, "ipc_handoff_bytes", ipc_spy)
        with pytest.raises(OptionWriterError, match="symlink"):
            _writer(lake).heal_partition(swapped, quarantine_to=tmp_path / "q" / "b.parquet")
        assert spy.calls == 0
        assert ipc_spy.calls == 0

    def test_escaping_dest_merge_refused_with_zero_crate_and_ipc_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        assert _write(lake, [_bar(minute=0)]) == 1
        outside = tmp_path / "outside" / "2021-01.parquet"
        outside.parent.mkdir(parents=True)
        frame = _render_df(_COORD, [_bar(minute=5)])
        spy = _RenderSpy()
        ipc_spy = _IpcSpy()
        _arm(monkeypatch, _fake_crate(defaults_render=spy))
        monkeypatch.setattr(lrr, "ipc_handoff_bytes", ipc_spy)
        with pytest.raises(OptionWriterError, match="outside the options tree"):
            _writer(lake).merge_partition_frame(outside, frame)
        assert spy.calls == 0
        assert ipc_spy.calls == 0


# ---------------------------------------------------------------------------
# R9 — the round-13 dtype-gate refusal precedes any render
# ---------------------------------------------------------------------------


class TestR9DtypeGateRefusalPrecedesRender:
    def test_nested_side_keyed_removal_refused_with_zero_crate_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        lake = tmp_path / "lake"
        dest = lake / "options" / "SERIES" / "2021-01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array([["CE"], ["PE"]], type=pa.list_(pa.string())),
                    "timestamp": pa.array(
                        [ts0, datetime(2021, 1, 4, 4, 1, tzinfo=UTC)],
                        type=pa.timestamp("us", tz="UTC"),
                    ),
                }
            ),
            dest,
        )
        before = dest.read_bytes()
        spy = _RenderSpy()
        ipc_spy = _IpcSpy()
        _arm(monkeypatch, _fake_crate(defaults_render=spy))
        monkeypatch.setattr(lrr, "ipc_handoff_bytes", ipc_spy)
        with pytest.raises(OptionWriterError, match="unclassifiable"):
            _writer(lake).heal_partition(
                dest, remove_keys={("CE", ts0)}, quarantine_to=tmp_path / "q" / "b.parquet"
            )
        assert dest.read_bytes() == before  # refused byte-untouched
        assert spy.calls == 0
        assert ipc_spy.calls == 0
