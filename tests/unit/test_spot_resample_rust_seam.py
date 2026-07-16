"""Seam-wiring pins: the Rust batch-derive gate inside ``spot_resample``.

DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 (the ADR-0020 row-12 fold). Stem-matched
to ``brutex/data/spot_resample.py`` (the discovery-gate law; the
``test_tf_resampler_rust_seam.py`` precedent) so the module's 100%
line+branch gates cover the seam branch: the OFF (default) byte-identity
bomb-spy, the ON-with-absent-wheel oracle degrade (no new crash class), the
validation-precedes-the-seam ordering law, the routed-through-the-seam spy,
and the derived-TF tuple's key-set/ORDER law vs the Python arm. The facade's
own suite is ``tests/unit/test_lake_rust_resample_siblings.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import lake_rust_resample as lrs
from brutex.data import spot_resample, tf_resampler
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the seam latch + burn-in budget + warn memo around every test."""
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


def _df_1m() -> pl.DataFrame:
    """A tiny valid 1-minute spot frame (two bars at the 09:15 IST open)."""
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


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: object | None) -> None:
    """Install a fake crate (or a poisoned ``None`` -> ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class TestDerivedTfTuple:
    def test_tuple_is_the_python_arms_key_set_and_order(self) -> None:
        # The rust arm iterates DERIVED_INTRADAY_TF_MINUTES; the python arm
        # iterates the resample_all_21_tfs dict — the tuple must be that
        # iteration minus {1m base, 1d daily}, ORDER included.
        expected = tuple(m for m in ALL_TF_MINUTES if m not in (1, DAILY_TF_MINUTES))
        assert expected == spot_resample.DERIVED_INTRADAY_TF_MINUTES
        assert len(spot_resample.DERIVED_INTRADAY_TF_MINUTES) == 19


class TestSeamWiring:
    def test_flag_off_never_touches_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The OFF (default) byte-identity spy-pin: with the flag unset the
        # public fn must never call the batch seam — a bomb proves it.
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> dict[int, pl.DataFrame]:
            raise AssertionError("seam must not be reached with the flag OFF")

        monkeypatch.setattr(spot_resample, "derive_frames_via_rust", _bomb)
        out = spot_resample.derive_intraday_tfs(_df_1m())
        expected = spot_resample._derive_intraday_tfs_python(_df_1m())
        assert list(out) == list(expected)
        for label, frame in expected.items():
            assert out[label].schema == frame.schema
            assert out[label].equals(frame)

    def test_flag_on_with_absent_wheel_serves_the_python_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No new crash class: flag ON + no wheel → every frame serves from
        # the per-TF Python oracle through the facade's import-guard degrade,
        # and the mapping's key ORDER matches the Python arm exactly.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        _install_crate(monkeypatch, None)
        out = spot_resample.derive_intraday_tfs(_df_1m())
        expected = spot_resample._derive_intraday_tfs_python(_df_1m())
        assert list(out) == list(expected)
        for label, frame in expected.items():
            assert out[label].schema == frame.schema
            assert out[label].equals(frame)

    def test_validation_raises_before_the_seam_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # VALIDATION STAYS PYTHON: the rust arm runs _validate_input FIRST,
        # so a schema defect raises the identical error class on both arms
        # and the batch seam is never touched.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> dict[int, pl.DataFrame]:
            raise AssertionError("seam must not be reached for invalid input")

        monkeypatch.setattr(spot_resample, "derive_frames_via_rust", _bomb)
        with pytest.raises(TypeError):
            spot_resample.derive_intraday_tfs("not-a-frame")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="missing required columns"):
            spot_resample.derive_intraday_tfs(_df_1m().drop("volume"))

    def test_flag_on_routes_through_the_batch_seam_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[int, ...]] = []

        def _spy(df: pl.DataFrame, tfs: Sequence[int], *, oracle: Any) -> dict[int, pl.DataFrame]:
            seen.append(tuple(tfs))
            assert oracle is tf_resampler._resample_1m_to_tf_python
            return {tf: oracle(df, tf) for tf in tfs}

        monkeypatch.setattr(spot_resample, "derive_frames_via_rust", _spy)
        out = spot_resample.derive_intraday_tfs(_df_1m())
        assert seen == [spot_resample.DERIVED_INTRADAY_TF_MINUTES]
        expected = spot_resample._derive_intraday_tfs_python(_df_1m())
        assert list(out) == list(expected)
        for label, frame in expected.items():
            assert out[label].equals(frame)
