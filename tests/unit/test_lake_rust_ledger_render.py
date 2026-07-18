"""Unit matrix for :mod:`brutex.data.lake_rust_ledger_render` (DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001).

Flag parsing / import guard + SYMBOL handshake / polars version guard /
ROW-EQUIVALENCE burn-in + autorevert / degrade set / panic belt /
refuse-poison / reset seam — every facade path exercised WITHOUT the wheel
(fake crate modules injected via ``sys.modules``; "rust-like" bytes minted
through the installed polars wheel's OWN ``write_parquet(use_pyarrow=False)``
— the SAME Rust encoder family the crate links), so the 100% line+branch
gate holds on wheel-less CI. The REAL-crate reader-parity battery lives in
``tests/integration/test_window_ledger_rust_reader_parity.py`` and the
state-convergence NAMED MERGE GATE in
``tests/integration/test_window_ledger_rust_state_convergence.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import io
import sys
import types
from typing import TYPE_CHECKING, Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from brutex.data import lake_rust_ledger_render, lake_rust_render, window_ledger_store
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_NOW_ISO = "2026-06-08T12:00:00.000001+00:00"


def _rows(n: int = 3) -> list[tuple[Any, ...]]:
    """*n* well-formed 17-field ledger row tuples (the ``to_rows`` shape)."""
    out: list[tuple[Any, ...]] = []
    for i in range(n):
        out.append(
            (
                "groww",
                f"SEC{i:04}",
                "1minute",
                "2021-01-01",
                "2021-08-01",
                ("SUCCESS", "EMPTY", "FAILED", "PLANNED")[i % 4],
                i,
                1,
                ("boom" if i % 4 == 2 else None),
                _NOW_ISO,
                (None if i % 4 == 3 else _NOW_ISO),
                0,
                1,
                (None if i % 3 == 1 else i),
                (None if i % 3 == 1 else 0),
                (None if i % 3 == 1 else 0),
                (None if i % 3 == 1 else "live"),
            )
        )
    return out


def _oracle_bytes(rows: Sequence[tuple[Any, ...]]) -> bytes:
    """The pyarrow oracle render (the store's retained live oracle)."""
    return window_ledger_store._render_ledger_parquet_python(list(rows))


def _polars_bytes(rows: Sequence[tuple[Any, ...]]) -> bytes:
    """Row-equivalent "rust-like" bytes via the wheel's own Rust encoder."""
    frame = lake_rust_ledger_render._ledger_frame(list(rows), pl)
    buffer = io.BytesIO()
    frame.write_parquet(buffer, use_pyarrow=False)
    return buffer.getvalue()


def _fake_crate(
    render: object, *, pinned: object = None, omit_render: bool = False
) -> types.ModuleType:
    """A fake ``brutex_lake`` exposing the G10 API (or deliberately not)."""
    module = types.ModuleType("brutex_lake")
    if not omit_render:
        module.render_ledger_parquet_from_ipc = render  # type: ignore[attr-defined]
    version = pl.__version__ if pinned is None else pinned
    module.pinned_polars_version = lambda: version  # type: ignore[attr-defined]
    # ModuleType declares __version__ read-only to type checkers; the fake
    # needs one for the engaged-INFO's crate_version field.
    module.__dict__["__version__"] = "0.1.0-fake"
    return module


def _install(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    """Install *crate* as the importable ``brutex_lake`` (``None`` = absent)."""
    if crate is None:
        monkeypatch.setitem(sys.modules, "brutex_lake", None)  # import → ImportError
    else:
        monkeypatch.setitem(sys.modules, "brutex_lake", crate)


def _seam(rows: Sequence[tuple[Any, ...]] | None = None) -> bytes:
    the_rows = _rows() if rows is None else list(rows)
    return lake_rust_ledger_render.render_ledger_parquet_bytes(
        rows=the_rows, python_render=lambda: _oracle_bytes(the_rows)
    )


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Test isolation: env unset + latch/burn-in/memo reset both sides."""
    monkeypatch.delenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, raising=False)
    lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()
    yield
    lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " TRUE "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, raw)
        assert lake_rust_ledger_render.lake_rust_ledger_render_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, raw)
        with capture_logs() as logs:
            assert lake_rust_ledger_render.lake_rust_ledger_render_enabled() is False
        assert logs == []

    def test_unset_and_whitespace_default_off_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            assert lake_rust_ledger_render.lake_rust_ledger_render_enabled() is False
        monkeypatch.setenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, "   ")
        with capture_logs() as more:
            assert lake_rust_ledger_render.lake_rust_ledger_render_enabled() is False
        assert logs == [] and more == []

    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, "onn")
        with capture_logs() as logs:
            assert lake_rust_ledger_render.lake_rust_ledger_render_enabled() is False
            assert lake_rust_ledger_render.lake_rust_ledger_render_enabled() is False
        warns = [e for e in logs if e["event"] == "lake_rust_ledger_render.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == "onn"

    def test_flag_vocabulary_is_the_shared_sibling_classifier(self) -> None:
        # §11 COMMON: the tristate classifier is IMPORTED from the sibling
        # facade, never a copy — identity, not just equality.
        assert lake_rust_ledger_render.env_flag_tristate is lake_rust_render.env_flag_tristate

    def test_env_name_and_constants_pinned(self) -> None:
        assert (
            lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV == "BRUTEX_LAKE_RUST_LEDGER_RENDER"
        )
        assert lake_rust_ledger_render.BURN_IN_SAMPLES == 16
        assert lake_rust_ledger_render._CRATE_API == (
            "render_ledger_parquet_from_ipc",
            "pinned_polars_version",
        )


class TestSchemaDriftPins:
    def test_ledger_columns_match_the_store(self) -> None:
        # The facade's column order MUST equal the store's `_COLUMNS` (the
        # db_view `_LEDGER_ROW_COLUMNS` pin-not-import precedent — no cycle).
        assert lake_rust_ledger_render.LEDGER_COLUMNS == window_ledger_store._COLUMNS

    def test_string_int_split_matches_the_oracle_schema(self) -> None:
        # The facade's String/Int64 split MUST mirror `_rows_to_table`'s
        # pyarrow schema — pa.string() ↔ pl.String, pa.int64() ↔ pl.Int64.
        table = window_ledger_store._rows_to_table(_rows(4))
        for field in table.schema:
            if field.name in lake_rust_ledger_render._LEDGER_STRING_COLUMNS:
                assert field.type == pa.string(), field.name
            else:
                assert field.type == pa.int64(), field.name
        assert set(lake_rust_ledger_render._LEDGER_STRING_COLUMNS) | {
            n
            for n in lake_rust_ledger_render.LEDGER_COLUMNS
            if n not in lake_rust_ledger_render._LEDGER_STRING_COLUMNS
        } == set(lake_rust_ledger_render.LEDGER_COLUMNS)

    def test_ledger_frame_builds_exact_dtypes_and_empty(self) -> None:
        frame = lake_rust_ledger_render._ledger_frame(_rows(5), pl)
        assert frame.columns == list(lake_rust_ledger_render.LEDGER_COLUMNS)
        for name in lake_rust_ledger_render.LEDGER_COLUMNS:
            want = pl.String if name in lake_rust_ledger_render._LEDGER_STRING_COLUMNS else pl.Int64
            assert frame.schema[name] == want, name
        empty = lake_rust_ledger_render._ledger_frame([], pl)
        assert empty.height == 0 and empty.columns == list(lake_rust_ledger_render.LEDGER_COLUMNS)


class TestResetSeamAndLatch:
    def test_reset_restores_fresh_process_view(self) -> None:
        lake_rust_ledger_render._disable("poison")
        lake_rust_ledger_render._warn_once("stale")
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()
        assert lake_rust_ledger_render.rust_ledger_render_disabled() is False
        assert (
            lake_rust_ledger_render._BURN_IN_REMAINING[0] == lake_rust_ledger_render.BURN_IN_SAMPLES
        )
        assert set() == lake_rust_ledger_render._OBSERVABILITY_MEMO

    def test_reset_remaining_zero_is_steady_state(self) -> None:
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests(remaining=0)
        assert lake_rust_ledger_render._BURN_IN_REMAINING[0] == 0

    def test_warn_once_suppresses_repeats(self) -> None:
        with capture_logs() as logs:
            lake_rust_ledger_render._warn_once("reason_x", detail=1)
            lake_rust_ledger_render._warn_once("reason_x", detail=2)
        assert len(logs) == 1

    def test_disable_latch_is_set_once(self) -> None:
        with capture_logs() as logs:
            lake_rust_ledger_render._disable("first")
            lake_rust_ledger_render._disable("second")
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "first"
        assert len(logs) == 1
        assert logs[0]["event"] == "lake_rust_ledger_render.rust_path_disabled"

    def test_disable_error_logs_autoreverted(self) -> None:
        with capture_logs() as logs:
            lake_rust_ledger_render._disable("bad", error=True)
        assert logs[0]["event"] == "lake_rust_ledger_render.render_autoreverted"
        assert logs[0]["log_level"] == "error"


class TestImportGuard:
    def test_absent_wheel_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, None)
        with capture_logs() as logs:
            served = _seam()
        assert served == _oracle_bytes(_rows())
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "crate_unavailable"
        assert logs[0]["event"] == "lake_rust_ledger_render.rust_path_disabled"

    def test_pre_g10_wheel_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wheel that imports but LACKS the G10 render symbol is a pre-G10
        # install — the SYMBOL handshake (the G8 law), never AttributeError.
        _install(monkeypatch, _fake_crate(None, omit_render=True))
        served = _seam()
        assert served == _oracle_bytes(_rows())
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "crate_stale"

    def test_latched_seam_short_circuits_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def _bomb(ipc: bytes) -> bytes:
            calls.append(1)
            return b"never"

        _install(monkeypatch, _fake_crate(_bomb))
        lake_rust_ledger_render._disable("already")
        assert _seam() == _oracle_bytes(_rows())
        assert calls == []

    def test_polars_unavailable_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _fake_crate(lambda ipc: b"x"))
        monkeypatch.setitem(sys.modules, "polars", None)  # import polars → ImportError
        assert _seam() == _oracle_bytes(_rows())
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "polars_unavailable"


class TestVersionGuard:
    def test_metadata_raise_latches_crate_metadata_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate(lambda ipc: b"x")

        def _boom() -> str:
            raise RuntimeError("foreign install")

        crate.pinned_polars_version = _boom  # type: ignore[attr-defined]
        _install(monkeypatch, crate)
        assert _seam() == _oracle_bytes(_rows())
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "crate_metadata_error"

    def test_metadata_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = _fake_crate(lambda ipc: b"x")

        def _oom() -> str:
            raise MemoryError

        crate.pinned_polars_version = _oom  # type: ignore[attr-defined]
        _install(monkeypatch, crate)
        with pytest.raises(MemoryError):
            _seam()

    def test_version_mismatch_latches_naming_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _fake_crate(lambda ipc: b"x", pinned="9.99.9"))
        with capture_logs() as logs:
            assert _seam() == _oracle_bytes(_rows())
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "polars_version_mismatch"
        entry = logs[0]
        assert entry["installed_polars"] == pl.__version__
        assert entry["crate_pinned_polars"] == "9.99.9"


class TestRenderPathDegrades:
    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(ipc: bytes) -> bytes:
            raise ValueError("bad payload")

        _install(monkeypatch, _fake_crate(_raise))
        with capture_logs() as logs:
            assert _seam() == _oracle_bytes(_rows())
            assert _seam() == _oracle_bytes(_rows())
        warns = [e for e in logs if e["event"] == "lake_rust_ledger_render.fallback"]
        assert len(warns) == 1 and warns[0]["reason"] == "render_error"
        # A degrade is NOT a latch — the next call may retry.
        assert lake_rust_ledger_render.rust_ledger_render_disabled() is False

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        def _raise(ipc: bytes) -> bytes:
            raise exc

        _install(monkeypatch, _fake_crate(_raise))
        with pytest.raises(exc):
            _seam()

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(ipc: bytes) -> bytes:
            raise ZeroDivisionError("panic-shaped")

        _install(monkeypatch, _fake_crate(_raise))
        assert _seam() == _oracle_bytes(_rows())
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "crate_panic"

    @pytest.mark.parametrize("poison", ["not-bytes", b""])
    def test_refuse_poison_non_bytes_or_empty(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _install(monkeypatch, _fake_crate(lambda ipc: poison))
        with capture_logs() as logs:
            assert _seam() == _oracle_bytes(_rows())
        warns = [e for e in logs if e["event"] == "lake_rust_ledger_render.fallback"]
        assert warns and warns[0]["reason"] == "render_result_refused"
        assert lake_rust_ledger_render.rust_ledger_render_disabled() is False


class TestBurnInRowEquivalence:
    def test_clean_burn_in_ships_rust_bytes_and_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _rows(6)
        rust_bytes = _polars_bytes(rows)
        _install(monkeypatch, _fake_crate(lambda ipc: rust_bytes))
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests(remaining=2)
        with capture_logs() as logs:
            first = _seam(rows)
            second = _seam(rows)
        assert first == rust_bytes and second == rust_bytes
        # The rust bytes genuinely DIFFER from the oracle bytes (the #1917
        # divergence class) — the seam served them on ROW equivalence.
        assert rust_bytes != _oracle_bytes(rows)
        events = [e["event"] for e in logs]
        assert "lake_rust_ledger_render.engaged" in events
        assert "lake_rust_ledger_render.burn_in_complete" in events
        assert lake_rust_ledger_render._BURN_IN_REMAINING[0] == 0

    def test_engaged_logs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = _rows(2)
        rust_bytes = _polars_bytes(rows)
        _install(monkeypatch, _fake_crate(lambda ipc: rust_bytes))
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests(remaining=5)
        with capture_logs() as logs:
            _seam(rows)
            _seam(rows)
        assert [e["event"] for e in logs].count("lake_rust_ledger_render.engaged") == 1

    def test_row_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _rows(4)
        wrong = list(rows)
        wrong[1] = (*rows[1][:6], 999_999, *rows[1][7:])  # a divergent `bars` cell
        rust_bytes = _polars_bytes(wrong)
        _install(monkeypatch, _fake_crate(lambda ipc: rust_bytes))
        with capture_logs() as logs:
            served = _seam(rows)
        assert served == _oracle_bytes(rows)
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "burn_in_row_mismatch"
        errors = [e for e in logs if e["event"] == "lake_rust_ledger_render.render_autoreverted"]
        assert errors and errors[0]["mismatch"] == "rows"

    def test_schema_mismatch_timestamp_dtype_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE critical watch-item: a crate render that re-typed
        # `last_attempt_at` into a timestamp dtype must refuse — the DuckDB
        # `last_attempt_at >= ?` VARCHAR-compare surface depends on it. The
        # string-family normalization must NOT fold a timestamp.
        rows = _rows(3)
        frame = lake_rust_ledger_render._ledger_frame(rows, pl).with_columns(
            pl.col("last_attempt_at").str.to_datetime("%Y-%m-%dT%H:%M:%S%.f%:z")
        )
        buffer = io.BytesIO()
        frame.write_parquet(buffer, use_pyarrow=False)
        _install(monkeypatch, _fake_crate(lambda ipc: buffer.getvalue()))
        served = _seam(rows)
        assert served == _oracle_bytes(rows)
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "burn_in_row_mismatch"

    def test_undecodable_rust_bytes_autorevert_compare_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_crate(lambda ipc: b"PAR1 definitely not parquet PAR1"))
        rows = _rows(2)
        served = _seam(rows)
        assert served == _oracle_bytes(rows)
        assert lake_rust_ledger_render._DISABLED_REASON[0] == "burn_in_compare_error"

    def test_compare_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = _rows(2)
        _install(monkeypatch, _fake_crate(lambda ipc: _polars_bytes(rows)))

        def _oom(rust_bytes: bytes, oracle_bytes: bytes) -> str | None:
            raise MemoryError

        monkeypatch.setattr(lake_rust_ledger_render, "_row_equivalence_mismatch", _oom)
        with pytest.raises(MemoryError):
            _seam(rows)

    def test_steady_state_ships_rust_bytes_with_no_oracle_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _rows(3)
        rust_bytes = _polars_bytes(rows)
        _install(monkeypatch, _fake_crate(lambda ipc: rust_bytes))
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests(remaining=0)
        oracle_calls: list[int] = []

        def _spy_oracle() -> bytes:
            oracle_calls.append(1)
            return _oracle_bytes(rows)

        served = lake_rust_ledger_render.render_ledger_parquet_bytes(
            rows=rows, python_render=_spy_oracle
        )
        assert served == rust_bytes
        assert oracle_calls == []  # steady state — no oracle render, no compare

    def test_empty_rows_burn_in_holds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The EMPTY-ledger watch-item at the facade layer: a 0-row render is
        # row-equivalent (0 rows, same 16-col normalized schema) even though
        # polars writes 0 row groups where pyarrow writes 1.
        rows: list[tuple[Any, ...]] = []
        rust_bytes = _polars_bytes(rows)
        _install(monkeypatch, _fake_crate(lambda ipc: rust_bytes))
        served = _seam(rows)
        assert served == rust_bytes
        assert lake_rust_ledger_render.rust_ledger_render_disabled() is False


class TestRowEquivalenceHelpers:
    def test_normalized_dtypes_fold_string_family_only(self) -> None:
        schema = pa.schema(
            [
                ("a", pa.large_string()),
                ("b", pa.string_view()),
                ("c", pa.string()),
                ("d", pa.int64()),
                ("e", pa.timestamp("us", tz="UTC")),
            ]
        )
        folded = lake_rust_ledger_render._normalized_dtypes(schema)
        assert folded[0] == ("a", pa.string())
        assert folded[1] == ("b", pa.string())
        assert folded[2] == ("c", pa.string())
        assert folded[3] == ("d", pa.int64())
        assert folded[4] == ("e", pa.timestamp("us", tz="UTC"))  # NEVER folded

    def test_row_equivalence_mismatch_verdicts(self) -> None:
        rows = _rows(4)
        assert (
            lake_rust_ledger_render._row_equivalence_mismatch(
                _polars_bytes(rows), _oracle_bytes(rows)
            )
            is None
        )
        other = _rows(4)
        other[0] = (*other[0][:7], 42, *other[0][8:])  # divergent `attempts`
        assert (
            lake_rust_ledger_render._row_equivalence_mismatch(
                _polars_bytes(other), _oracle_bytes(rows)
            )
            == "rows"
        )
        # Schema verdict: drop a column on one side.
        table = window_ledger_store._rows_to_table(rows).drop_columns(["transient_retries"])
        buffer = io.BytesIO()
        pq.write_table(table, buffer)  # type: ignore[no-untyped-call, unused-ignore]
        assert (
            lake_rust_ledger_render._row_equivalence_mismatch(
                buffer.getvalue(), _oracle_bytes(rows)
            )
            == "schema"
        )
