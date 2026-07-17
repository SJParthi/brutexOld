"""Seam-wiring pins: the fused session-cutoff route in ``resample_session_to_tf`` (T11).

Stem-matched to ``brutex/data/fno_resample.py`` so the module's 100%
line+branch gates cover the DEC-LAKE-RUST-RESAMPLE-DAILY-001 session
branch: the OFF (default) byte-identity bomb-spy, the routed-with-the-
composite-oracle spy, the no-cutoff arm's session-face avoidance, the
empty-frame short-circuit, validation-precedes-the-seam, and the
stale-wheel composite degrade (result equals the OFF arm exactly). The
facade's own session suite is ``tests/unit/test_lake_rust_resample.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import datetime as dt
import sys
import types
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import fno_resample
from brutex.data import lake_rust_resample as lrs

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


def _day_df(minutes: int = 8) -> pl.DataFrame:
    """One IST session's LAKE-shaped frame (``timestamp`` column) from 09:15."""
    open_utc = dt.datetime(2024, 1, 2, 3, 45, tzinfo=dt.UTC)
    instants = [open_utc + dt.timedelta(minutes=i) for i in range(minutes)]
    return pl.DataFrame(
        {
            "timestamp": instants,
            "open": [100.0 + i for i in range(minutes)],
            "high": [110.0 + i for i in range(minutes)],
            "low": [90.0 + i for i in range(minutes)],
            "close": [105.0 + i for i in range(minutes)],
            "volume": [10 + i for i in range(minutes)],
            "open_interest": [1_000 + i for i in range(minutes)],
        }
    ).with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))


def _legacy_session(day_df: pl.DataFrame, tf: int, cutoff_min: int | None) -> pl.DataFrame:
    """The verbatim pre-T11 composite (resample + Python filter) — the oracle law."""
    if day_df.is_empty():
        return day_df
    renamed = day_df.rename({"timestamp": "ts_utc"})
    resampled = fno_resample._resample_1m_to_tf_python(renamed, tf)
    if cutoff_min is None or resampled.is_empty():
        return resampled
    return fno_resample._forming_bucket_cutoff(resampled, tf, cutoff_min)


class TestSessionSeamWiring:
    def test_flag_off_never_touches_the_session_face(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("session face must not be reached with the flag OFF")

        monkeypatch.setattr(fno_resample, "resample_session_frame_via_rust", _bomb)
        out = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=562)
        expected = _legacy_session(_day_df(), 5, 562)
        assert out.schema == expected.schema
        assert out.equals(expected)
        # The forming 09:20 bucket dropped, the closed 09:15 bucket kept.
        assert out.height == 1

    def test_flag_on_with_cutoff_routes_through_the_session_face(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[tuple[int, int]] = []

        def _spy(
            df: pl.DataFrame, tf: int, cutoff: int, *, oracle: Any
        ) -> pl.DataFrame:
            seen.append((tf, cutoff))
            assert "ts_utc" in df.columns, "the lake frame is RENAMED before the seam"
            return oracle(df, tf)

        monkeypatch.setattr(fno_resample, "resample_session_frame_via_rust", _spy)
        out = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=562)
        assert seen == [(5, 562)]
        expected = _legacy_session(_day_df(), 5, 562)
        assert out.schema == expected.schema
        assert out.equals(expected), "the composite oracle IS the legacy law"

    def test_flag_on_without_cutoff_avoids_the_session_face(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # cutoff_min=None (a whole-day read) keeps the plain
        # resample_1m_to_tf path — the session face exists ONLY for the
        # fused resample+cutoff law.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_lake", None)  # single face degrades

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("session face must not be reached without a cutoff")

        monkeypatch.setattr(fno_resample, "resample_session_frame_via_rust", _bomb)
        out = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=None)
        expected = _legacy_session(_day_df(), 5, None)
        assert out.equals(expected)

    def test_empty_frame_short_circuits_before_validation_and_the_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("session face must not be reached for an empty frame")

        monkeypatch.setattr(fno_resample, "resample_session_frame_via_rust", _bomb)
        empty = _day_df().clear()
        out = fno_resample.resample_session_to_tf(empty, 5, cutoff_min=562)
        assert out.is_empty()
        assert "timestamp" in out.columns, "the empty INPUT frame returns unchanged"

    def test_validation_raises_before_the_session_face_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("session face must not be reached for invalid input")

        monkeypatch.setattr(fno_resample, "resample_session_frame_via_rust", _bomb)
        with pytest.raises(ValueError, match="Unsupported target TF"):
            fno_resample.resample_session_to_tf(_day_df(), 999, cutoff_min=562)

    def test_stale_wheel_composite_degrade_equals_the_off_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A G5-era wheel (no resample_session_ipc): the facade serves the
        # COMPOSITE oracle — the result must equal the flag-OFF arm exactly
        # (the retained-oracle-arm law), across cutoffs incl. boundaries.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        module = types.ModuleType("brutex_lake")
        module.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
        module.linked_zstd_version = lambda: "1.5.7"  # type: ignore[attr-defined]
        module.resample_1m_ipc = lambda ipc, tf, oi: b""  # type: ignore[attr-defined]
        module.resample_all_ipc = lambda ipc, tfs, oi: []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_lake", module)
        for cutoff in (0, 555, 560, 562, 929):
            out = fno_resample.resample_session_to_tf(_day_df(), 5, cutoff_min=cutoff)
            expected = _legacy_session(_day_df(), 5, cutoff)
            assert out.schema == expected.schema, f"cutoff={cutoff}"
            assert out.equals(expected), f"cutoff={cutoff}"
