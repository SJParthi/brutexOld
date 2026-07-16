"""Unit suite for the sibling facade entry points of ``lake_rust_resample``.

DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001: ``derive_frames_via_rust`` (the batch
19-TF derive), ``daily_restamp_via_rust`` (the daily filter + midnight-IST
restamp stage) and ``session_cutoff_via_rust`` (the forming-bucket cutoff
filter). Stem-matched (``test_lake_rust_resample*``) so the module's 100%
line+branch gate collects it beside the #1600 suite. Every test injects a
FAKE ``brutex_lake`` module straight into ``sys.modules`` — the real-wheel
parity corpus lives in
``tests/integration/test_lake_rust_resample_siblings_parity.py``.
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
from brutex.data import lake_rust_resample as lrs
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the latch + burn-in budget + warn memo around every test."""
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


#: The frame the ORACLE stubs return — any recognisable frame works; the
#: seams never inspect it beyond the burn-in comparison.
_ORACLE_DF = pl.DataFrame({"ts_utc": [1], "close": [105.0]})

#: A value-DIFFERENT frame (burn-in value-mismatch arm).
_WRONG_DF = pl.DataFrame({"ts_utc": [1], "close": [999.0]})

#: The fixture cutoff date crossing the daily seam.
_BEFORE = dt.date(2024, 1, 2)


def _ipc(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_ipc(buf, compression="uncompressed")
    return buf.getvalue()


def _fake_crate(
    *,
    pinned: Any = pl.__version__,
    batch: Any = None,
    daily: Any = None,
    cutoff: Any = None,
) -> types.ModuleType:
    """Build a fake ``brutex_lake`` module with injectable sibling behaviour."""
    module = types.ModuleType("brutex_lake")
    module.pinned_polars_version = lambda: pinned  # type: ignore[attr-defined]
    module.linked_zstd_version = lambda: "1.5.7"  # type: ignore[attr-defined]
    module.resample_1m_ipc = lambda ipc, tf, oi: _ipc(_ORACLE_DF)  # type: ignore[attr-defined]
    if batch is None:
        batch = lambda ipc, tfs, oi: [_ipc(_ORACLE_DF) for _ in tfs]  # noqa: E731
    module.resample_all_ipc = batch  # type: ignore[attr-defined]
    if daily is None:
        daily = lambda ipc, before_days, keep_oi: _ipc(_ORACLE_DF)  # noqa: E731
    module.daily_restamp_ipc = daily  # type: ignore[attr-defined]
    if cutoff is None:
        cutoff = lambda ipc, tf, cutoff_min: _ipc(_ORACLE_DF)  # noqa: E731
    module.session_cutoff_ipc = cutoff  # type: ignore[attr-defined]
    return module


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> None:
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


class _CountingTfOracle:
    """Per-TF oracle stub counting invocations (returns :data:`_ORACLE_DF`)."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, df_1m: Any, target_tf_minutes: int) -> pl.DataFrame:
        self.calls.append(target_tf_minutes)
        return _ORACLE_DF


class _CountingDailyOracle:
    """Daily oracle stub counting invocations (returns :data:`_ORACLE_DF`)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, daily: Any, before: dt.date) -> pl.DataFrame:
        self.calls += 1
        return _ORACLE_DF


class _CountingCutoffOracle:
    """Cutoff oracle stub counting invocations (returns :data:`_ORACLE_DF`)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, resampled: Any, tf: int, cutoff_min: int) -> pl.DataFrame:
        self.calls += 1
        return _ORACLE_DF


_TFS = (5, 15)


def _run_derive(
    monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None
) -> tuple[dict[int, pl.DataFrame], _CountingTfOracle]:
    _install_crate(monkeypatch, module)
    oracle = _CountingTfOracle()
    return lrs.derive_frames_via_rust(_df_1m(), _TFS, oracle=oracle), oracle


def _run_daily(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    *,
    keep_oi: bool = True,
) -> tuple[pl.DataFrame, _CountingDailyOracle]:
    _install_crate(monkeypatch, module)
    oracle = _CountingDailyOracle()
    return (
        lrs.daily_restamp_via_rust(_df_1m(), _BEFORE, keep_oi=keep_oi, oracle=oracle),
        oracle,
    )


def _run_cutoff(
    monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None
) -> tuple[pl.DataFrame, _CountingCutoffOracle]:
    _install_crate(monkeypatch, module)
    oracle = _CountingCutoffOracle()
    return lrs.session_cutoff_via_rust(_df_1m(), 5, 560, oracle=oracle), oracle


def _assert_oracle_dict(out: dict[int, pl.DataFrame]) -> None:
    assert list(out) == list(_TFS)
    for frame in out.values():
        assert frame.equals(_ORACLE_DF)


# ---------------------------------------------------------------------------
# guard layers (shared _guarded_crate — each sibling degrades identically)
# ---------------------------------------------------------------------------


class TestGuardsServeTheOracle:
    def test_absent_wheel_latches_and_serves_all_three(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out, oracle = _run_derive(monkeypatch, None)
        _assert_oracle_dict(out)
        assert oracle.calls == list(_TFS)
        assert lrs._DISABLED_REASON[0] == "crate_unavailable"
        daily_out, daily_oracle = _run_daily(monkeypatch, None)
        assert daily_out.equals(_ORACLE_DF)
        assert daily_oracle.calls == 1
        cutoff_out, cutoff_oracle = _run_cutoff(monkeypatch, None)
        assert cutoff_out.equals(_ORACLE_DF)
        assert cutoff_oracle.calls == 1

    def test_version_mismatch_latches_before_any_sibling_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(*_args: object) -> bytes:
            raise AssertionError("no sibling call past a version mismatch")

        crate = _fake_crate(pinned="9.99.9", batch=_bomb, daily=_bomb, cutoff=_bomb)
        out, _oracle = _run_derive(monkeypatch, crate)
        _assert_oracle_dict(out)
        assert lrs._DISABLED_REASON[0] == "polars_version_mismatch"

    def test_latched_process_short_circuits_every_sibling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs._DISABLED_REASON[0] = "crate_panic"
        out, oracle = _run_derive(monkeypatch, _fake_crate())
        _assert_oracle_dict(out)
        assert oracle.calls == list(_TFS)
        daily_out, _ = _run_daily(monkeypatch, _fake_crate())
        assert daily_out.equals(_ORACLE_DF)
        cutoff_out, _ = _run_cutoff(monkeypatch, _fake_crate())
        assert cutoff_out.equals(_ORACLE_DF)


# ---------------------------------------------------------------------------
# derive_frames_via_rust
# ---------------------------------------------------------------------------


class TestDeriveFramesViaRust:
    def test_clean_burn_in_serves_the_crate_frames_per_tf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = lrs._BURN_IN_REMAINING[0]
        out, oracle = _run_derive(monkeypatch, _fake_crate())
        _assert_oracle_dict(out)
        # Burn-in ran per TF (each consumed budget + compared vs the oracle).
        assert oracle.calls == list(_TFS)
        assert lrs._BURN_IN_REMAINING[0] == before - len(_TFS)
        assert lrs._DISABLED_REASON[0] is None

    def test_steady_state_serves_crate_frames_with_no_oracle_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        out, oracle = _run_derive(monkeypatch, _fake_crate())
        _assert_oracle_dict(out)
        assert oracle.calls == []

    def test_crate_raise_degrades_the_whole_derive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object) -> list[bytes]:
            raise ValueError("boom")

        with capture_logs() as logs:
            out, oracle = _run_derive(monkeypatch, _fake_crate(batch=_raise))
        _assert_oracle_dict(out)
        assert oracle.calls == list(_TFS)
        assert lrs._DISABLED_REASON[0] is None  # degrade, not a latch
        assert any(log.get("reason") == "derive_error" for log in logs)

    @pytest.mark.parametrize(
        "payloads",
        [
            12345,  # not a list
            [b"x"],  # wrong length (2 TFs requested)
            "nope",  # not a list (str)
        ],
    )
    def test_poisoned_batch_shape_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, payloads: object
    ) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_derive(
                monkeypatch, _fake_crate(batch=lambda ipc, tfs, oi: payloads)
            )
        _assert_oracle_dict(out)
        assert any(log.get("reason") == "derive_result_refused" for log in logs)

    def test_non_bytes_element_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_derive(
                monkeypatch,
                _fake_crate(batch=lambda ipc, tfs, oi: [_ipc(_ORACLE_DF), 42]),
            )
        _assert_oracle_dict(out)
        assert any(log.get("reason") == "derive_result_refused" for log in logs)

    def test_undecodable_element_degrades_the_whole_derive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_derive(
                monkeypatch,
                _fake_crate(batch=lambda ipc, tfs, oi: [_ipc(_ORACLE_DF), b"garbage"]),
            )
        _assert_oracle_dict(out)
        assert any(log.get("reason") == "result_decode_error" for log in logs)

    def test_burn_in_mismatch_autoreverts_and_serves_the_whole_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First TF matches, second diverges: the mid-derive autorevert must
        # discard EVERY crate frame — never a partial rust/python mix.
        def _batch(ipc: bytes, tfs: list[int], oi: bool) -> list[bytes]:
            return [_ipc(_ORACLE_DF), _ipc(_WRONG_DF)]

        with capture_logs() as logs:
            out, _oracle = _run_derive(monkeypatch, _fake_crate(batch=_batch))
        _assert_oracle_dict(out)
        assert lrs._DISABLED_REASON[0] == "burn_in_frame_mismatch"
        assert any(log["event"] == "lake_rust_resample.autoreverted" for log in logs)

    def test_raising_zstd_metadata_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate()

        def _boom() -> str:
            raise RuntimeError("zstd probe broken")

        crate.linked_zstd_version = _boom  # type: ignore[attr-defined]
        out, _oracle = _run_derive(monkeypatch, crate)
        _assert_oracle_dict(out)
        assert lrs._DISABLED_REASON[0] == "crate_metadata_error"

    def test_memoryerror_re_raises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(*_args: object) -> list[bytes]:
            raise MemoryError

        _install_crate(monkeypatch, _fake_crate(batch=_oom))
        with pytest.raises(MemoryError):
            lrs.derive_frames_via_rust(_df_1m(), _TFS, oracle=_CountingTfOracle())


# ---------------------------------------------------------------------------
# daily_restamp_via_rust
# ---------------------------------------------------------------------------


class TestDailyRestampViaRust:
    def test_clean_burn_in_serves_the_crate_frame_and_crosses_days(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[int, bool]] = []

        def _daily(ipc: bytes, before_days: int, keep_oi: bool) -> bytes:
            seen.append((before_days, keep_oi))
            return _ipc(_ORACLE_DF)

        out, oracle = _run_daily(monkeypatch, _fake_crate(daily=_daily), keep_oi=True)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1  # burn-in reference
        # 2024-01-02 is 19_724 days past the Unix epoch.
        assert seen == [(19_724, True)]

    def test_keep_oi_false_crosses_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[bool] = []

        def _daily(ipc: bytes, before_days: int, keep_oi: bool) -> bytes:
            seen.append(keep_oi)
            return _ipc(_ORACLE_DF)

        _run_daily(monkeypatch, _fake_crate(daily=_daily), keep_oi=False)
        assert seen == [False]

    def test_crate_raise_degrades_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object) -> bytes:
            raise ValueError("boom")

        with capture_logs() as logs:
            out, oracle = _run_daily(monkeypatch, _fake_crate(daily=_raise))
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert any(log.get("reason") == "daily_restamp_error" for log in logs)

    def test_non_bytes_result_is_refuse_poison(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_daily(
                monkeypatch, _fake_crate(daily=lambda ipc, b, k: 12345)
            )
        assert out.equals(_ORACLE_DF)
        assert any(log.get("reason") == "daily_restamp_result_refused" for log in logs)

    def test_undecodable_result_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_daily(
                monkeypatch, _fake_crate(daily=lambda ipc, b, k: b"garbage")
            )
        assert out.equals(_ORACLE_DF)
        assert any(log.get("reason") == "result_decode_error" for log in logs)

    def test_burn_in_mismatch_autoreverts_and_ships_the_oracle_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_daily(
                monkeypatch, _fake_crate(daily=lambda ipc, b, k: _ipc(_WRONG_DF))
            )
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "burn_in_frame_mismatch"
        assert any(log["event"] == "lake_rust_resample.autoreverted" for log in logs)

    def test_raising_zstd_metadata_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate()

        def _boom() -> str:
            raise RuntimeError("zstd probe broken")

        crate.linked_zstd_version = _boom  # type: ignore[attr-defined]
        out, oracle = _run_daily(monkeypatch, crate)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1

    def test_steady_state_serves_the_crate_frame_with_no_oracle_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        out, oracle = _run_daily(monkeypatch, _fake_crate())
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 0


# ---------------------------------------------------------------------------
# session_cutoff_via_rust
# ---------------------------------------------------------------------------


class TestSessionCutoffViaRust:
    def test_clean_burn_in_serves_the_crate_frame_and_crosses_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[int, int]] = []

        def _cutoff(ipc: bytes, tf: int, cutoff_min: int) -> bytes:
            seen.append((tf, cutoff_min))
            return _ipc(_ORACLE_DF)

        out, oracle = _run_cutoff(monkeypatch, _fake_crate(cutoff=_cutoff))
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert seen == [(5, 560)]

    def test_crate_raise_degrades_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object) -> bytes:
            raise ValueError("boom")

        with capture_logs() as logs:
            out, oracle = _run_cutoff(monkeypatch, _fake_crate(cutoff=_raise))
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1
        assert any(log.get("reason") == "session_cutoff_error" for log in logs)

    def test_non_bytes_result_is_refuse_poison(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_cutoff(
                monkeypatch, _fake_crate(cutoff=lambda ipc, t, c: None)
            )
        assert out.equals(_ORACLE_DF)
        assert any(log.get("reason") == "session_cutoff_result_refused" for log in logs)

    def test_undecodable_result_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_cutoff(
                monkeypatch, _fake_crate(cutoff=lambda ipc, t, c: b"garbage")
            )
        assert out.equals(_ORACLE_DF)
        assert any(log.get("reason") == "result_decode_error" for log in logs)

    def test_burn_in_mismatch_autoreverts_and_ships_the_oracle_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            out, _oracle = _run_cutoff(
                monkeypatch, _fake_crate(cutoff=lambda ipc, t, c: _ipc(_WRONG_DF))
            )
        assert out.equals(_ORACLE_DF)
        assert lrs._DISABLED_REASON[0] == "burn_in_frame_mismatch"
        assert any(log["event"] == "lake_rust_resample.autoreverted" for log in logs)

    def test_raising_zstd_metadata_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crate = _fake_crate()

        def _boom() -> str:
            raise RuntimeError("zstd probe broken")

        crate.linked_zstd_version = _boom  # type: ignore[attr-defined]
        out, oracle = _run_cutoff(monkeypatch, crate)
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 1

    def test_steady_state_serves_the_crate_frame_with_no_oracle_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        out, oracle = _run_cutoff(monkeypatch, _fake_crate())
        assert out.equals(_ORACLE_DF)
        assert oracle.calls == 0


# ---------------------------------------------------------------------------
# drift pins
# ---------------------------------------------------------------------------


class TestSiblingApiPins:
    def test_crate_api_includes_the_sibling_faces(self) -> None:
        assert "daily_restamp_ipc" in lrs._CRATE_API
        assert "session_cutoff_ipc" in lrs._CRATE_API

    def test_sibling_names_are_registered_in_the_shim(self) -> None:
        # The shim's __all__ mirrors the pymodule registration; every
        # _CRATE_API member must be re-exported (source pin — no wheel).
        from pathlib import Path

        shim = (
            Path(__file__).resolve().parents[2]
            / "rust"
            / "brutex-lake"
            / "python"
            / "brutex_lake"
            / "__init__.py"
        ).read_text(encoding="utf-8")
        for name in lrs._CRATE_API:
            assert f'"{name}"' in shim, name

    def test_epoch_date_is_the_unix_epoch(self) -> None:
        assert lrs._EPOCH_DATE == dt.date(1970, 1, 1)
