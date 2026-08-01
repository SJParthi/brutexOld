"""Seam-wiring pins: the Rust resample gate inside ``tf_resampler`` (slice G5).

Stem-matched to ``brutex/data/tf_resampler.py`` (the discovery-gate law; the
``test_feed_replay_rust_seam.py`` precedent) so the module's 100%
line+branch gates cover the DEC-LAKE-RUST-RESAMPLE-001 seam branch: the OFF
(default) byte-identity bomb-spy, the ON-with-absent-wheel oracle degrade
(no new crash class), the validation/membership-precede-the-seam ordering
law, and the routed-through-the-seam spy. The facade's own suite is
``tests/unit/test_lake_rust_resample.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from brutex.data import lake_rust_resample as lrs
from brutex.data import tf_resampler

if TYPE_CHECKING:
    from collections.abc import Iterator


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
    """Reset the seam latch + burn-in budget + warn memo around every test."""
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


def _df_1m() -> pl.DataFrame:
    """A tiny valid 1-minute frame (two bars at the 09:15 IST open)."""
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


class TestSeamWiring:
    def test_flag_off_never_touches_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The OFF byte-identity spy-pin: with the flag at "0" (the kill
        # switch — default flipped ON, DEC-RUST-DEFAULT-ON-WAVE-001) the public fn must
        # never call the seam — a bomb proves it.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "0")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("seam must not be reached with the flag OFF")

        monkeypatch.setattr(tf_resampler, "resample_frame_via_rust", _bomb)
        out = tf_resampler.resample_1m_to_tf(_df_1m(), 5)
        assert out.equals(tf_resampler._resample_1m_to_tf_python(_df_1m(), 5))

    def test_flag_on_with_absent_wheel_serves_the_python_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No new crash class: flag ON + no wheel → the oracle (verbatim
        # Python body) serves through the seam's import-guard degrade.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        _install_crate(monkeypatch, None)
        out = tf_resampler.resample_1m_to_tf(_df_1m(), 5)
        assert out.equals(tf_resampler._resample_1m_to_tf_python(_df_1m(), 5))
        assert out.schema == tf_resampler._resample_1m_to_tf_python(_df_1m(), 5).schema

    def test_validation_and_membership_raise_before_the_seam_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")

        def _bomb(*_args: object, **_kwargs: object) -> pl.DataFrame:
            raise AssertionError("seam must not be reached for invalid input")

        monkeypatch.setattr(tf_resampler, "resample_frame_via_rust", _bomb)
        with pytest.raises(ValueError, match="Unsupported target TF"):
            tf_resampler.resample_1m_to_tf(_df_1m(), 999)
        with pytest.raises(TypeError):
            tf_resampler.resample_1m_to_tf("not-a-frame", 5)

    def test_flag_on_routes_through_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        seen: list[int] = []

        def _spy(df: pl.DataFrame, tf: int, *, oracle: Any) -> pl.DataFrame:
            seen.append(tf)
            assert oracle is tf_resampler._resample_1m_to_tf_python
            return oracle(df, tf)

        monkeypatch.setattr(tf_resampler, "resample_frame_via_rust", _spy)
        out = tf_resampler.resample_1m_to_tf(_df_1m(), 5)
        assert seen == [5]
        assert out.equals(tf_resampler._resample_1m_to_tf_python(_df_1m(), 5))
