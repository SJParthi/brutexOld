"""Unit gate for :mod:`brutex.meta.annotate` (DEC-META-LABEL-ANNOTATE-001 +
DEC-ABSTAIN-GATE-STREAM-001).

Real-writer fixtures (``write_tuple_sidecars``) drive the pass end-to-end:
the canonical ``.meta.json`` fourth sidecar, the run summary, idempotent
byte-identity, dry-run zero-write, per-stem isolation (counted skips, never
an abort), lens degrade arms, VIX-band best-effort reads, and the
never-touch law over every pre-existing artifact byte.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data.models import CandleBar
from brutex.engine.minute_path import PathStatus
from brutex.engine.session_lens import SessionLens
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.engine.trade_store import sidecar_paths_for, sweep_run_dir, write_tuple_sidecars
from brutex.meta.abstain import ABSTAIN_REASON_ORDER
from brutex.meta.annotate import (
    META_SCHEMA_VERSION,
    META_SIDECAR_SUFFIX,
    META_SUMMARY_FILENAME,
    MetaAnnotationReport,
    _lens_for,
    _sidecar_paths,
    _vix_band,
    annotate_run,
)
from brutex.meta.features import FEATURE_NAMES
from brutex.strategies.types import Side

RUN_ID = "grid-abc123"
COMBO = "combo-" + "a" * 10
BASE_ENTRY = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)  # 09:30 IST


def make_capture(run_id: str = COMBO, n: int = 40) -> ComboCapture:
    trades = []
    for i in range(n):
        entry = BASE_ENTRY + timedelta(days=i % 200, minutes=7 * (i % 13))
        pnl = 500 if (i * 7) % 3 == 0 else -400
        trades.append(
            CapturedTrade(
                entry_ts=entry,
                entry_px=Decimal("100.00"),
                exit_ts=entry + timedelta(minutes=30 + (i % 5)),
                exit_px=Decimal("101.00"),
                direction=Side.BUY,
                lots=None,
                exit_reason="take_profit" if pnl > 0 else "stop_loss",
                pnl_paisa=pnl,
                costs_paisa=None,
                mae_paisa=10,
                mfe_paisa=20,
                mae_1m_paisa=None,
                mfe_1m_paisa=None,
                path_1m_status=PathStatus.NO_1M_DATA,
                vix_entry=Decimal("14.5") if i % 4 else None,
                vix_exit=None,
            )
        )
    return ComboCapture(
        run_id=run_id,
        trades=tuple(trades),
        equity=(),
        equity_status=PathStatus.NO_1M_DATA,
        vix_missing_trades=sum(1 for t in trades if t.vix_entry is None),
        parity_ok=True,
        parity_detail="",
    )


def seed_fixture(
    lake: Path,
    *,
    run_id: str = RUN_ID,
    instrument: str = "NSE-NIFTY",
    stem: str = "tuple-abc",
    captures: list[ComboCapture] | None = None,
) -> Path:
    """Write one real sidecar trio under ``<lake>/sweep_runs/<run_id>``."""
    sweeps = lake / "sweeps"
    store = sweeps / instrument / "5minute" / f"{stem}.parquet"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.touch()
    paths = sidecar_paths_for(store, sweeps, run_id=run_id)
    write_tuple_sidecars(
        paths,
        captures if captures is not None else [make_capture()],
        [],
        store_content_hash="a" * 64,
    )
    return sweeps


def tree_digest(root: Path) -> str:
    """A deterministic digest of every file byte under *root*."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def read_meta(lake: Path, *, stem: str = "tuple-abc", instrument: str = "NSE-NIFTY") -> Any:
    meta = (
        lake
        / "sweep_runs"
        / RUN_ID
        / "results-depth"
        / instrument
        / "5minute"
        / f"{stem}{META_SIDECAR_SUFFIX}"
    )
    return json.loads(meta.read_text(encoding="utf-8"))


class TestAnnotateRun:
    def test_happy_path_writes_the_fourth_sidecar_and_summary(self, tmp_path: Path) -> None:
        sweeps = seed_fixture(tmp_path)
        report = annotate_run(sweeps, RUN_ID)
        assert isinstance(report, MetaAnnotationReport)
        assert report.ok is True
        assert report.stems_total == report.stems_annotated == 1
        assert report.combos_annotated == 1
        assert report.trades_annotated == 40
        payload = read_meta(tmp_path)
        assert payload["schema_version"] == META_SCHEMA_VERSION
        assert payload["dec"] == "DEC-META-LABEL-ANNOTATE-001"
        assert payload["sweep_run_id"] == RUN_ID
        assert payload["instrument"] == "NSE-NIFTY"
        assert payload["tf"] == "5minute"
        assert payload["revalidation_required"] is True
        assert payload["notes"]  # the honest-limits notes ship verbatim
        combo = payload["combos"][COMBO]
        assert combo["n_trades"] == 40
        assert combo["feature_names"] == list(FEATURE_NAMES)
        assert combo["n_thresholds_swept"] == 10
        assert set(combo["kept_pct_at"]) == {"0.6", "0.7", "0.8"}
        assert len(combo["p_win"]) == 40
        assert all(p is None or 0.0 <= p <= 1.0 for p in combo["p_win"].values())
        abstain = combo["abstain"]
        assert abstain["dec"] == "DEC-ABSTAIN-GATE-STREAM-001"
        assert list(abstain["gate_reasons_counts"]) == sorted(ABSTAIN_REASON_ORDER)
        assert abstain["would_abstain_count"] + abstain["kept_trades"] == 40
        summary = json.loads((tmp_path / "sweep_runs" / RUN_ID / META_SUMMARY_FILENAME).read_text())
        assert summary["stems_annotated"] == 1
        assert report.summary_path is not None

    def test_existing_artifacts_stay_byte_untouched(self, tmp_path: Path) -> None:
        # The R13 zero-risk law: the pass ADDS files, it never rewrites one.
        sweeps = seed_fixture(tmp_path)
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        annotate_run(sweeps, RUN_ID)
        for path, content in before.items():
            assert path.read_bytes() == content, f"{path} was rewritten"

    def test_rerun_is_byte_identical(self, tmp_path: Path) -> None:
        sweeps = seed_fixture(tmp_path)
        annotate_run(sweeps, RUN_ID)
        digest = tree_digest(tmp_path)
        annotate_run(sweeps, RUN_ID)
        assert tree_digest(tmp_path) == digest

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        sweeps = seed_fixture(tmp_path)
        digest = tree_digest(tmp_path)
        report = annotate_run(sweeps, RUN_ID, dry_run=True)
        assert tree_digest(tmp_path) == digest
        assert report.dry_run is True
        assert report.ok is True
        assert report.stems_total == 1
        assert report.combos_annotated == 1
        assert report.summary_path is None
        assert report.outcomes[0].reason == "dry_run"

    def test_stamp_invalid_is_a_counted_skip(self, tmp_path: Path) -> None:
        sweeps = seed_fixture(tmp_path)
        stamp = next((tmp_path / "sweep_runs").rglob("*.stamp.json"))
        stamp.write_text("{corrupt", encoding="utf-8")
        report = annotate_run(sweeps, RUN_ID)
        assert report.ok is False
        assert report.stems_skipped == 1
        assert report.outcomes[0].reason == "stamp_invalid"
        # The summary still writes (the pass never aborts).
        assert (tmp_path / "sweep_runs" / RUN_ID / META_SUMMARY_FILENAME).is_file()

    def test_no_stems_is_a_clean_empty_report(self, tmp_path: Path) -> None:
        sweeps = tmp_path / "sweeps"
        (tmp_path / "sweep_runs" / RUN_ID).mkdir(parents=True)
        report = annotate_run(sweeps, RUN_ID)
        assert report.ok is True
        assert report.stems_total == 0

    def test_per_stem_isolation_counts_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stem 1 poisoned (unreadable trades bytes) — stem 2 still annotates.
        sweeps = seed_fixture(tmp_path, stem="tuple-aaa")
        seed_fixture(tmp_path, stem="tuple-bbb")
        import brutex.meta.annotate as annotate_module

        real = annotate_module._combo_annotation

        def poisoned(paths: Any, combo_id: str, lens: Any, scorer: Any) -> Any:
            if "tuple-aaa" in str(paths.trades):
                raise RuntimeError("planted stem poison")
            return real(paths, combo_id, lens, scorer)

        monkeypatch.setattr(annotate_module, "_combo_annotation", poisoned)
        report = annotate_run(sweeps, RUN_ID)
        assert report.stems_total == 2
        assert report.stems_annotated == 1
        assert report.stems_skipped == 1
        failed = next(o for o in report.outcomes if not o.annotated)
        assert failed.reason == "annotate_failed:RuntimeError"
        assert report.ok is False

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_global_stops_reraise_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        sweeps = seed_fixture(tmp_path)
        import brutex.meta.annotate as annotate_module

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise exc_type()

        monkeypatch.setattr(annotate_module, "_combo_annotation", boom)
        with pytest.raises(exc_type):
            annotate_run(sweeps, RUN_ID)

    def test_malformed_rows_are_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sweeps = seed_fixture(tmp_path)
        import brutex.meta.annotate as annotate_module
        from brutex.meta.features import trade_features_from_wire as real_extract

        def flaky(row: Any, *, lens: Any) -> Any:
            if row["trade_seq"] == 0:
                return None  # one malformed row
            return real_extract(row, lens=lens)

        monkeypatch.setattr(annotate_module, "trade_features_from_wire", flaky)
        report = annotate_run(sweeps, RUN_ID)
        assert report.ok is True
        assert report.outcomes[0].malformed_rows == 1
        combo = read_meta(tmp_path)["combos"][COMBO]
        assert combo["n_malformed_rows"] == 1
        assert combo["n_trades"] == 39

    def test_confident_scorer_yields_passing_trades(self, tmp_path: Path) -> None:
        # A high-confidence scorer clears the τ door: passing trades carry
        # NO per-trade reasons entry (EMIT-ON-PRESENT) and count as kept.
        sweeps = seed_fixture(tmp_path)

        def confident(
            flat: list[float], n_features: int, weights: list[float], bias: float
        ) -> list[float]:
            return [0.9] * (len(flat) // n_features)

        report = annotate_run(sweeps, RUN_ID, scorer=confident)
        assert report.ok is True
        abstain = read_meta(tmp_path)["combos"][COMBO]["abstain"]
        assert abstain["would_abstain_count"] == 0
        assert abstain["kept_trades"] == 40
        assert abstain["kept_pct"] == 1.0
        assert abstain["per_trade_reasons"] == {}
        assert all(count == 0 for count in abstain["gate_reasons_counts"].values())

    def test_multi_combo_stem_annotates_every_combo_sorted(self, tmp_path: Path) -> None:
        captures = [make_capture("combo-zz", 5), make_capture("combo-aa", 5)]
        sweeps = seed_fixture(tmp_path, captures=captures)
        report = annotate_run(sweeps, RUN_ID)
        assert report.combos_annotated == 2
        payload = read_meta(tmp_path)
        assert list(payload["combos"]) == ["combo-aa", "combo-zz"]
        # 5 trades < MIN_TRADES_FOR_FIT → honest degenerate annotation.
        combo = payload["combos"]["combo-aa"]
        assert combo["oof"]["degenerate_reason"] == "too_few_trades"
        assert all(p is None for p in combo["p_win"].values())


class TestLensArms:
    def test_lens_built_from_daily_bars_and_cached(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def daily_bars_for(instrument: str) -> list[CandleBar]:
            calls.append(instrument)
            bars = []
            for i in range(6):
                ts = datetime(2024, 1, 1, 0, 0, tzinfo=UTC) + timedelta(days=i)
                px = Decimal(100 + i)
                bars.append(
                    CandleBar(
                        timestamp=ts,
                        open=px,
                        high=px + 1,
                        low=px - 1,
                        close=px,
                        volume=0,
                        open_interest=None,
                    )
                )
            return bars

        cache: dict[str, SessionLens] = {}
        lens = _lens_for("NSE-NIFTY", cache, daily_bars_for)
        assert lens.trend_by_date  # a real (possibly all-SIDEWAYS) fold
        again = _lens_for("NSE-NIFTY", cache, daily_bars_for)
        assert again is lens
        assert calls == ["NSE-NIFTY"]  # built once, cached

    def test_unsorted_daily_bars_degrade_to_an_empty_lens(self) -> None:
        ts = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
        bars = [
            CandleBar(
                timestamp=ts,
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=0,
                open_interest=None,
            ),
            CandleBar(
                timestamp=ts - timedelta(days=1),  # NOT strictly increasing
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=0,
                open_interest=None,
            ),
        ]
        lens = _lens_for("NSE-NIFTY", {}, lambda _instrument: bars)
        assert lens.trend_by_date == {}
        assert lens.event_flags_by_date == {}

    def test_no_loader_and_none_bars_read_empty(self) -> None:
        assert _lens_for("X", {}, None).trend_by_date == {}
        assert _lens_for("Y", {}, lambda _instrument: None).trend_by_date == {}

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_global_stops_reraise_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        import brutex.meta.annotate as annotate_module

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise exc_type()

        monkeypatch.setattr(annotate_module, "build_session_lens", boom)
        with pytest.raises(exc_type):
            _lens_for("X", {}, None)


class TestVixBand:
    def test_missing_aggs_reads_none(self, tmp_path: Path) -> None:
        paths = _sidecar_paths(tmp_path / "a" / "b" / "x.trades.parquet")
        assert _vix_band(paths, "any") == (None, None)

    def test_absent_row_reads_none(self, tmp_path: Path) -> None:
        import polars as pl

        aggs = tmp_path / "x.aggs.parquet"
        pl.DataFrame(
            {"run_id": ["other"], "vix_band_lo": [12.0], "vix_band_hi": [20.0]}
        ).write_parquet(aggs)
        paths = _sidecar_paths(tmp_path / "x.trades.parquet")
        assert _vix_band(paths, "combo") == (None, None)

    def test_real_band_reads_floats(self, tmp_path: Path) -> None:
        import polars as pl

        aggs = tmp_path / "x.aggs.parquet"
        pl.DataFrame(
            {"run_id": ["combo"], "vix_band_lo": [12.5], "vix_band_hi": [19.5]}
        ).write_parquet(aggs)
        paths = _sidecar_paths(tmp_path / "x.trades.parquet")
        assert _vix_band(paths, "combo") == (12.5, 19.5)

    def test_null_band_reads_none(self, tmp_path: Path) -> None:
        import polars as pl

        aggs = tmp_path / "x.aggs.parquet"
        pl.DataFrame(
            {
                "run_id": ["combo"],
                "vix_band_lo": pl.Series([None], dtype=pl.Float64),
                "vix_band_hi": pl.Series([None], dtype=pl.Float64),
            }
        ).write_parquet(aggs)
        paths = _sidecar_paths(tmp_path / "x.trades.parquet")
        assert _vix_band(paths, "combo") == (None, None)

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_global_stops_reraise_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        import polars as pl

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise exc_type()

        monkeypatch.setattr(pl, "scan_parquet", boom)
        paths = _sidecar_paths(tmp_path / "x.trades.parquet")
        with pytest.raises(exc_type):
            _vix_band(paths, "combo")


class TestSidecarPaths:
    def test_trio_paths_rebuild_from_the_trades_file(self, tmp_path: Path) -> None:
        trades = tmp_path / "NSE-NIFTY" / "5minute" / "tuple-x.trades.parquet"
        paths = _sidecar_paths(trades)
        assert paths.trades == trades
        assert paths.aggs.name == "tuple-x.aggs.parquet"
        assert paths.equity.name == "tuple-x.equity.parquet"
        assert paths.stamp.name == "tuple-x.stamp.json"
        # Mirror-pinned against the engine's own path law.
        sweeps = tmp_path / "sweeps"
        store = sweeps / "NSE-NIFTY" / "5minute" / "tuple-x.parquet"
        engine_paths = sidecar_paths_for(store, sweeps, run_id=RUN_ID)
        rebuilt = _sidecar_paths(engine_paths.trades)
        assert rebuilt == engine_paths

    def test_run_dir_rides_the_one_seam(self, tmp_path: Path) -> None:
        assert sweep_run_dir(tmp_path / "sweeps", RUN_ID) == tmp_path / "sweep_runs" / RUN_ID
