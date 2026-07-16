"""ON-vs-OFF parity corpus for the 4 resampler-sibling Rust ports.

DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001: drives the REAL public entry points of
the four ported modules both arms — flag OFF (the verbatim Python bodies) vs
flag ON (the ``brutex_lake`` crate through the sibling facades, in the
post-burn-in steady state with the Python ORACLES BOMBED so any silent
degrade fails loudly):

* ``spot_resample.derive_intraday_tfs`` — all 19 derived intraday TFs;
* ``fno_resample.derive_fno_intraday_tfs`` — OI + Greeks-overlay carry;
* ``fno_resample.resample_session_to_tf`` — the forming-bucket cutoff law
  across whole-session / closed-bucket / mid-bucket / all-forming cutoffs;
* ``spot_daily_resample.resample_1m_to_daily`` +
  ``fno_daily_resample.resample_1m_to_fno_daily`` — CandleBar LISTS compared
  attribute-for-attribute (the frame stage is rust; materialisation stays
  Python on both arms).

Frame equality is DTYPE-EXACT (schema equality AND ``DataFrame.equals`` —
the #1795b comparator). Requires the built wheel; skips (with the named
remedy) when the crate is absent, pre-siblings (no restamp face), or
version-guard-mismatched — the lake-rust-parity.yml lane force-aligns the
wheel so none of those skips can fire there.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

brutex_lake = pytest.importorskip(
    "brutex_lake",
    reason=(
        "brutex-lake crate not installed — the sibling parity corpus runs in "
        "the lake-rust-parity.yml lane (or locally after `pip install rust/brutex-lake`)"
    ),
)
pl = pytest.importorskip("polars")

from brutex.data import (  # noqa: E402  # noqa: E402
    fno_daily_resample,
    fno_resample,
    spot_daily_resample,
    spot_resample,
    tf_resampler,
)
from brutex.data import lake_rust_resample as lrs  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

if not hasattr(brutex_lake, "daily_restamp_ipc"):
    pytest.skip(
        "installed brutex_lake wheel predates the resampler-sibling faces — "
        "rebuild the wheel from rust/brutex-lake (pip install rust/brutex-lake)",
        allow_module_level=True,
    )

if brutex_lake.pinned_polars_version() != pl.__version__:
    pytest.skip(
        "installed polars does not match the crate pin — align them per "
        "docs/runbooks/lake-rust-render-lockstep.md",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the seam latch + burn-in budget + warn memo around every test."""
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
    raise AssertionError(
        "the Python oracle must NOT run on the steady-state ON arm — a "
        "degrade fired where the crate frame should have served"
    )


#: 2024-01-01 09:15 IST session open, as a UTC instant.
_OPEN_D1 = datetime(2024, 1, 1, 3, 45, tzinfo=UTC)


def _spot_frame() -> pl.DataFrame:
    """Two IST sessions (30 + 20 bars, a mid-session hole) + a pre-open stray."""
    stamps: list[datetime] = [_OPEN_D1 - timedelta(minutes=7)]  # 09:08 IST stray
    stamps += [_OPEN_D1 + timedelta(minutes=i) for i in range(30) if i not in (11, 12, 13)]
    day2 = _OPEN_D1 + timedelta(days=1)
    stamps += [day2 + timedelta(minutes=i) for i in range(20)]
    n = len(stamps)
    return pl.DataFrame(
        {
            "ts_utc": stamps,
            "open": [100.0 + 0.05 * i for i in range(n)],
            "high": [101.0 + 0.05 * i for i in range(n)],
            "low": [99.0 + 0.05 * i for i in range(n)],
            "close": [100.5 + 0.05 * i for i in range(n)],
            "volume": [0 if i % 7 == 0 else 10 + i for i in range(n)],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us")))


def _fno_frame() -> pl.DataFrame:
    """The spot corpus + carry-last OI (null tail) + one Greeks-overlay column."""
    base = _spot_frame()
    n = base.height
    oi = [1_000 + 3 * i for i in range(n)]
    oi_series = pl.Series("open_interest", oi, dtype=pl.Int64)
    # Null the LAST bar's OI — the documented honest carry-last edge.
    oi_series = oi_series.scatter(n - 1, None)
    iv = pl.Series("iv", [0.2 + 0.001 * i for i in range(n)], dtype=pl.Float64)
    return base.with_columns(oi_series, iv)


def _assert_frames_equal(left: pl.DataFrame, right: pl.DataFrame, context: str) -> None:
    assert left.schema == right.schema, context
    assert left.equals(right), context


class TestDeriveWrappersParity:
    def test_spot_derive_on_equals_off_across_the_19_tfs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = _spot_frame()
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        off = spot_resample.derive_intraday_tfs(frame)
        lrs.reset_lake_rust_resample_for_tests(remaining=0)  # steady state
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb)
        on = spot_resample.derive_intraday_tfs(frame)
        assert list(on) == list(off)
        assert len(on) == 19
        for label, expected in off.items():
            _assert_frames_equal(on[label], expected, f"spot derive tf={label}")

    def test_fno_derive_on_equals_off_with_oi_and_overlay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = _fno_frame()
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        off = fno_resample.derive_fno_intraday_tfs(frame)
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb)
        on = fno_resample.derive_fno_intraday_tfs(frame)
        assert list(on) == list(off)
        for label, expected in off.items():
            assert "open_interest" in on[label].columns
            assert "iv" in on[label].columns
            _assert_frames_equal(on[label], expected, f"fno derive tf={label}")

    def test_burn_in_comparator_passes_on_the_real_derive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fresh budget + REAL oracles: an armed burn-in over the real crate
        # must complete with NO autorevert (the anti-vacuous arm).
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        out = spot_resample.derive_intraday_tfs(_spot_frame())
        assert len(out) == 19
        assert lrs._DISABLED_REASON[0] is None


class TestSessionCutoffParity:
    @pytest.mark.parametrize("tf", [1, 5, 15, 60])
    @pytest.mark.parametrize("cutoff_min", [None, 559, 560, 570, 585, 1439])
    def test_on_equals_off_across_cutoffs(
        self, monkeypatch: pytest.MonkeyPatch, tf: int, cutoff_min: int | None
    ) -> None:
        day = _fno_frame().filter(
            (pl.col("ts_utc") + pl.duration(hours=5, minutes=30)).dt.date() == date(2024, 1, 1)
        )
        lake_day = day.rename({"ts_utc": "timestamp"})
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        off = fno_resample.resample_session_to_tf(lake_day, tf, cutoff_min=cutoff_min)
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setattr(fno_resample, "_session_cutoff_python", _bomb)
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb)
        on = fno_resample.resample_session_to_tf(lake_day, tf, cutoff_min=cutoff_min)
        _assert_frames_equal(on, off, f"session cutoff tf={tf} cutoff={cutoff_min}")

    def test_burn_in_comparator_passes_on_the_real_cutoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lake_day = _fno_frame().rename({"ts_utc": "timestamp"})
        out = fno_resample.resample_session_to_tf(lake_day, 5, cutoff_min=570)
        assert not out.is_empty()
        assert lrs._DISABLED_REASON[0] is None


class TestDailyResamplersParity:
    @pytest.mark.parametrize(
        "before",
        [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)],
        ids=["drops-all", "keeps-day1", "keeps-both"],
    )
    def test_spot_daily_bars_on_equals_off(
        self, monkeypatch: pytest.MonkeyPatch, before: date
    ) -> None:
        frame = _spot_frame()
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        off = spot_daily_resample.resample_1m_to_daily(frame, before=before)
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setattr(spot_daily_resample, "_daily_restamp_frame_python", _bomb)
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb)
        on = spot_daily_resample.resample_1m_to_daily(frame, before=before)
        assert on == off
        for bar_on, bar_off in zip(on, off, strict=True):
            assert str(bar_on.open) == str(bar_off.open)
            assert bar_on.timestamp.tzinfo == bar_off.timestamp.tzinfo

    @pytest.mark.parametrize(
        "before",
        [date(2024, 1, 2), date(2024, 1, 3)],
        ids=["keeps-day1", "keeps-both"],
    )
    def test_fno_daily_bars_on_equals_off_including_null_oi_edge(
        self, monkeypatch: pytest.MonkeyPatch, before: date
    ) -> None:
        frame = _fno_frame()
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        off = fno_daily_resample.resample_1m_to_fno_daily(frame, before=before)
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setattr(fno_daily_resample, "_fno_daily_restamp_oracle", _bomb)
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb)
        on = fno_daily_resample.resample_1m_to_fno_daily(frame, before=before)
        assert on == off
        # The last session's LAST 1m bar carries a null OI — both arms must
        # read the honest None (the documented carry-last edge).
        if before == date(2024, 1, 3):
            assert on[-1].open_interest is None
        # Decimal repr edges stay Python-side and identical on both arms.
        for bar_on, bar_off in zip(on, off, strict=True):
            assert str(bar_on.close) == str(bar_off.close)
            assert bar_on.timestamp.tzinfo == bar_off.timestamp.tzinfo

    def test_burn_in_comparator_passes_on_the_real_daily_restamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        spot_bars = spot_daily_resample.resample_1m_to_daily(_spot_frame(), before=date(2024, 1, 3))
        fno_bars = fno_daily_resample.resample_1m_to_fno_daily(
            _fno_frame(), before=date(2024, 1, 3)
        )
        assert len(spot_bars) == 2
        assert len(fno_bars) == 2
        assert lrs._DISABLED_REASON[0] is None
