"""Seam-wiring pins: the Rust batch-derive + session-cutoff gates in ``fno_resample``.

DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 (the ADR-0020 row-13 fold — "Inherits #11
+ the forming-bucket filter pinned"). Stem-matched to
``brutex/data/fno_resample.py`` (the discovery-gate law) so the module's 100%
line+branch gates cover BOTH seam branches: the OFF (default) byte-identity
bomb-spies, the ON-with-absent-wheel oracle degrades (no new crash class),
and the routed-through-the-seam spies. The facade's own suite is
``tests/unit/test_lake_rust_resample_siblings.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import fno_resample, tf_resampler
from brutex.data import lake_rust_resample as lrs

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the seam latch + burn-in budget + warn memo around every test."""
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


def _df_1m() -> pl.DataFrame:
    """A tiny valid F&O 1-minute frame (OI-carrying, resampler column names)."""
    import datetime as dt

    return pl.DataFrame(
        {
            "ts_utc": [dt.datetime(2024, 1, 1, 3, 45), dt.datetime(2024, 1, 1, 3, 46)],
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "volume": [10, 11],
            "open_interest": [1000, 1001],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us")))


def _day_df() -> pl.DataFrame:
    """A lake-shaped session frame (``timestamp`` column — the read-path name)."""
    return _df_1m().rename({"ts_utc": "timestamp"})


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: object | None) -> None:
    """Install a fake crate (or a poisoned ``None`` -> ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class TestDeriveSeamWiring:
    def test_flag_off_never_touches_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> dict[int, pl.DataFrame]:
            raise AssertionError("seam must not be reached with the flag OFF")

        monkeypatch.setattr(fno_resample, "derive_frames_via_rust", _bomb)
        out = fno_resample.derive_fno_intraday_tfs(_df_1m())
        expected = fno_resample._derive_fno_intraday_tfs_python(_df_1m())
        assert list(out) == list(expected)
        for label, frame in expected.items():
            assert out[label].schema == frame.schema
            assert out[label].equals(frame)

    def test_flag_on_with_absent_wheel_serves_the_python_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        _install_crate(monkeypatch, None)
        out = fno_resample.derive_fno_intraday_tfs(_df_1m())
        expected = fno_resample._derive_fno_intraday_tfs_python(_df_1m())
        assert list(out) == list(expected)
        for label, frame in expected.items():
            # The OI column rides the presence law on both arms.
            assert "open_interest" in out[label].columns
            assert out[label].schema == frame.schema
            assert out[label].equals(frame)

    def test_validation_raises_before_the_seam_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> dict[int, pl.DataFrame]:
            raise AssertionError("seam must not be reached for invalid input")

        monkeypatch.setattr(fno_resample, "derive_frames_via_rust", _bomb)
        with pytest.raises(TypeError):
            fno_resample.derive_fno_intraday_tfs("not-a-frame")  # type: ignore[arg-type]

    def test_flag_on_routes_through_the_batch_seam_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[int, ...]] = []

        def _spy(
            df: pl.DataFrame, tfs: Sequence[int], *, oracle: Any
        ) -> dict[int, pl.DataFrame]:
            seen.append(tuple(tfs))
            assert oracle is tf_resampler._resample_1m_to_tf_python
            return {tf: oracle(df, tf) for tf in tfs}

        monkeypatch.setattr(fno_resample, "derive_frames_via_rust", _spy)
        out = fno_resample.derive_fno_intraday_tfs(_df_1m())
        assert seen == [fno_resample.DERIVED_INTRADAY_TF_MINUTES]
        assert list(out) == list(fno_resample._derive_fno_intraday_tfs_python(_df_1m()))


class TestSessionCutoffSeamWiring:
    def test_flag_off_never_touches_the_cutoff_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("cutoff seam must not be reached with the flag OFF")

        monkeypatch.setattr(fno_resample, "session_cutoff_via_rust", _bomb)
        out = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=560)
        renamed = _day_df().rename({"timestamp": "ts_utc"})
        expected = fno_resample._session_cutoff_python(
            tf_resampler.resample_1m_to_tf(renamed, 5), 5, 560
        )
        assert out.schema == expected.schema
        assert out.equals(expected)

    def test_flag_on_with_absent_wheel_serves_the_python_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        _install_crate(monkeypatch, None)
        out = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=560)
        # The 09:15 5m bucket closes at 09:20 (minute 560) — kept; nothing
        # else exists, so exactly one closed bucket survives.
        assert out.height == 1
        assert "open_interest" in out.columns

    def test_no_cutoff_and_empty_frames_never_touch_the_cutoff_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # cutoff_min=None (whole session) and the empty-input early return
        # both precede the seam even with the flag ON.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("cutoff seam must not be reached")

        monkeypatch.setattr(fno_resample, "session_cutoff_via_rust", _bomb)
        _install_crate(monkeypatch, None)
        whole = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=None)
        assert whole.height == 1
        empty = _day_df().head(0)
        assert fno_resample.resample_session_to_tf(empty, 5, cutoff_min=560).is_empty()

    def test_flag_on_routes_through_the_cutoff_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[int, int]] = []

        def _spy(
            resampled: pl.DataFrame, tf: int, cutoff: int, *, oracle: Any
        ) -> pl.DataFrame:
            seen.append((tf, cutoff))
            assert oracle is fno_resample._session_cutoff_python
            return oracle(resampled, tf, cutoff)

        monkeypatch.setattr(fno_resample, "session_cutoff_via_rust", _spy)
        out = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=559)
        assert seen == [(5, 559)]
        # 555 + 5 = 560 > 559 — the forming bucket is dropped.
        assert out.is_empty()
