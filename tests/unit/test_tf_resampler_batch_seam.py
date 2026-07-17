"""Seam-wiring pins: the batch-face route inside ``resample_all_21_tfs`` (T11).

Stem-matched to ``brutex/data/tf_resampler.py`` (the discovery-gate law; the
``test_tf_resampler_rust_seam.py`` precedent) so the module's 100%
line+branch gates cover the DEC-LAKE-RUST-RESAMPLE-DAILY-001 batch branch:
the OFF (default) byte-identity bomb-spy, the routed-through-the-facade
spy, the ONE-FFI-crossing count law, validation-precedes-the-seam, and the
transitive inheritance of the two derive wrappers
(``spot_resample.derive_intraday_tfs`` / ``fno_resample.derive_fno_intraday_tfs``).
The facade's own batch suite is ``tests/unit/test_lake_rust_resample.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import datetime as dt
import io
import sys
import types
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import fno_resample, spot_resample, tf_resampler
from brutex.data import lake_rust_resample as lrs
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES, TF_LABELS

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


def _df_1m() -> pl.DataFrame:
    """A tiny valid 1-minute frame (two bars at the 09:15 IST open)."""
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


def _ipc(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_ipc(buf, compression="uncompressed")
    return buf.getvalue()


def _python_all_21(df_1m: pl.DataFrame) -> dict[int, pl.DataFrame]:
    return {tf: tf_resampler._resample_1m_to_tf_python(df_1m, tf) for tf in ALL_TF_MINUTES}


class TestBatchSeamWiring:
    def test_flag_off_never_touches_the_batch_face(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> dict[int, pl.DataFrame]:
            raise AssertionError("batch face must not be reached with the flag OFF")

        monkeypatch.setattr(tf_resampler, "resample_all_via_rust", _bomb)
        out = tf_resampler.resample_all_21_tfs(_df_1m())
        expected = _python_all_21(_df_1m())
        assert list(out.keys()) == list(expected.keys())
        for tf in ALL_TF_MINUTES:
            assert out[tf].schema == expected[tf].schema
            assert out[tf].equals(expected[tf])

    def test_flag_on_routes_once_through_the_batch_face(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[int, ...]] = []

        def _spy(df: pl.DataFrame, tfs: Any, *, oracle: Any) -> dict[int, pl.DataFrame]:
            seen.append(tuple(tfs))
            assert oracle is tf_resampler._resample_1m_to_tf_python
            return {tf: oracle(df, tf) for tf in tfs}

        monkeypatch.setattr(tf_resampler, "resample_all_via_rust", _spy)
        out = tf_resampler.resample_all_21_tfs(_df_1m())
        assert seen == [tuple(ALL_TF_MINUTES)]
        assert list(out.keys()) == list(ALL_TF_MINUTES)

    def test_flag_on_with_absent_wheel_serves_the_python_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No new crash class: flag ON + no wheel → the whole derive from the
        # oracle through the facade's import-guard degrade.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        out = tf_resampler.resample_all_21_tfs(_df_1m())
        expected = _python_all_21(_df_1m())
        for tf in ALL_TF_MINUTES:
            assert out[tf].schema == expected[tf].schema
            assert out[tf].equals(expected[tf])

    def test_validation_raises_before_the_batch_face_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> dict[int, pl.DataFrame]:
            raise AssertionError("batch face must not be reached for invalid input")

        monkeypatch.setattr(tf_resampler, "resample_all_via_rust", _bomb)
        with pytest.raises(TypeError):
            tf_resampler.resample_all_21_tfs("not-a-frame")

    def test_one_ffi_crossing_for_the_whole_21_tf_derive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The load-bearing count law: ONE resample_all_ipc crossing, ZERO
        # resample_1m_ipc fan-out — proven with a fake crate whose batch
        # face answers with the ORACLE frames (so burn-in passes and the
        # derive equals the OFF arm value-for-value).
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        df = _df_1m()
        expected = _python_all_21(df)
        batch_calls: list[int] = []
        single_calls: list[int] = []

        module = types.ModuleType("brutex_lake")
        module.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
        module.linked_zstd_version = lambda: "1.5.7"  # type: ignore[attr-defined]

        def _batch(ipc: bytes, tfs: list[int], oi: bool) -> list[bytes]:
            batch_calls.append(len(tfs))
            assert oi is True
            return [_ipc(expected[tf]) for tf in tfs]

        def _single(ipc: bytes, tf: int, oi: bool) -> bytes:
            single_calls.append(tf)
            return _ipc(expected[tf])

        module.resample_all_ipc = _batch  # type: ignore[attr-defined]
        module.resample_1m_ipc = _single  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_lake", module)

        out = tf_resampler.resample_all_21_tfs(df)
        assert batch_calls == [len(ALL_TF_MINUTES)], "exactly ONE batch crossing"
        assert single_calls == [], "no per-TF fan-out through resample_1m_ipc"
        assert lrs.rust_resample_disabled() is False
        for tf in ALL_TF_MINUTES:
            assert out[tf].schema == expected[tf].schema, f"tf={tf}"
            assert out[tf].equals(expected[tf]), f"tf={tf}"


class TestDeriveWrappersInherit:
    @pytest.mark.parametrize(
        ("derive", "module"),
        [
            (spot_resample.derive_intraday_tfs, spot_resample),
            (fno_resample.derive_fno_intraday_tfs, fno_resample),
        ],
        ids=["spot", "fno"],
    )
    def test_derive_wrappers_ride_the_batch_face_transitively(
        self, monkeypatch: pytest.MonkeyPatch, derive: Any, module: Any
    ) -> None:
        # spot_resample.py is deliberately UNTOUCHED by T11 — its
        # inheritance of the batch face (via resample_all_21_tfs) is pinned
        # here; fno_resample's intraday derive inherits identically.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[int, ...]] = []

        def _spy(df: pl.DataFrame, tfs: Any, *, oracle: Any) -> dict[int, pl.DataFrame]:
            seen.append(tuple(tfs))
            return {tf: oracle(df, tf) for tf in tfs}

        monkeypatch.setattr(tf_resampler, "resample_all_via_rust", _spy)
        out = derive(_df_1m())
        assert seen == [tuple(ALL_TF_MINUTES)], "one batch crossing per derive"
        expected_labels = [
            TF_LABELS[tf] for tf in ALL_TF_MINUTES if tf not in (1, DAILY_TF_MINUTES)
        ]
        assert list(out.keys()) == expected_labels
