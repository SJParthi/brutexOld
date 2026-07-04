"""Unit tests for the measured lake-footprint multiplier + pre-run disk guard.

100% line+branch over ``brutex.data.lake_footprint``
(DEC-LAKE-FOOTPRINT-MEASURED-001): the golden stat-walk over KNOWN file sizes
(source ``1minute`` vs derived buckets, SPOT-side vs FNO), the multiplier
Decimal law (``None`` on zero source — never a fabricated ratio), the
``schema_meta`` persist/reload round-trip (measured_at + audit sample bytes),
the corrupted-sidecar honesty (invalid / non-positive stored multiplier reads
``None``), the guard arithmetic (observed vs the conservative 6.5x fallback,
need = estimate minus sunk on-disk bytes, the +10% margin), the
``BRUTEX_PREBUILD_DISK_GUARD=off`` escape hatch, and the FAIL-OPEN closure
(any evaluation error logs ``pipeline.prebuild.disk_guard_error`` and returns
``None`` — a guard bug never blocks the pipeline's data stage).

Pure filesystem + dict-backed meta fakes; no DuckDB / FastAPI / network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.lake_footprint import (
    DEFAULT_SAFETY_MARGIN,
    DISK_GUARD_ENV,
    FALLBACK_MULTIPLIER,
    META_DERIVED_BYTES,
    META_MEASURED_AT,
    META_MULTIPLIER_FNO,
    META_MULTIPLIER_OVERALL,
    META_MULTIPLIER_SPOT,
    META_SOURCE_BYTES,
    DiskGuardDecision,
    LakeFootprint,
    SegmentFootprint,
    disk_guard_enabled,
    evaluate_disk_guard,
    load_observed_multiplier,
    make_disk_guard,
    measure_lake_footprint,
    persist_footprint,
    record_lake_footprint,
)
from structlog.testing import capture_logs


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _seed_lake(lake: Path) -> None:
    """Golden fixture: KNOWN byte sizes in all four segment/interval buckets."""
    bars = lake / "bars"
    # SPOT-side source (INDEX segment, 1minute): 100 + 50 bytes.
    _write(bars / "NSE/INDEX/NSE-NIFTY/1minute/2025/01.parquet", 100)
    _write(bars / "NSE/INDEX/NSE-NIFTY/1minute/2025/02.parquet", 50)
    # SPOT-side derived (5minute + 1day): 30 + 20 bytes.
    _write(bars / "NSE/INDEX/NSE-NIFTY/5minute/2025/01.parquet", 30)
    _write(bars / "NSE/INDEX/NSE-NIFTY/1day/2025/01.parquet", 20)
    # FNO source: 200 bytes; FNO derived: 40 bytes.
    _write(bars / "NSE/FNO/NSE-NIFTY-30Jan25-FUT/1minute/2025/01.parquet", 200)
    _write(bars / "NSE/FNO/NSE-NIFTY-30Jan25-FUT/15minute/2025/01.parquet", 40)
    # Too-shallow path (not a partition file) — must be ignored.
    _write(bars / "NSE/INDEX/stray.parquet", 999)


class TestSegmentFootprint:
    def test_total_and_multiplier_quantized(self) -> None:
        seg = SegmentFootprint(source_1m_bytes=300, derived_bytes=100)
        assert seg.total_bytes == 400
        assert seg.multiplier == Decimal("1.3333")

    def test_zero_source_multiplier_is_honest_none(self) -> None:
        assert SegmentFootprint(source_1m_bytes=0, derived_bytes=10).multiplier is None

    def test_overall_sums_both_segments(self) -> None:
        fp = LakeFootprint(
            spot=SegmentFootprint(source_1m_bytes=150, derived_bytes=50),
            fno=SegmentFootprint(source_1m_bytes=200, derived_bytes=40),
        )
        assert fp.overall == SegmentFootprint(source_1m_bytes=350, derived_bytes=90)


class TestMeasureLakeFootprint:
    def test_golden_known_sizes(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        fp = measure_lake_footprint(tmp_path)
        assert fp.spot == SegmentFootprint(source_1m_bytes=150, derived_bytes=50)
        assert fp.fno == SegmentFootprint(source_1m_bytes=200, derived_bytes=40)
        assert fp.overall.multiplier == Decimal("1.2571")  # 440/350 half-up @4dp

    def test_missing_bars_root_reads_empty(self, tmp_path: Path) -> None:
        fp = measure_lake_footprint(tmp_path / "no-such-lake")
        assert fp.overall == SegmentFootprint(source_1m_bytes=0, derived_bytes=0)
        assert fp.overall.multiplier is None

    def test_vanished_file_is_skipped(self, tmp_path: Path) -> None:
        """A broken symlink stats like a mid-walk vanished file (OSError)."""
        _seed_lake(tmp_path)
        ghost = tmp_path / "bars/NSE/INDEX/NSE-NIFTY/1minute/2025/03.parquet"
        ghost.symlink_to(tmp_path / "bars/definitely-not-there.parquet")
        fp = measure_lake_footprint(tmp_path)
        assert fp.spot.source_1m_bytes == 150  # the ghost contributed nothing


class TestPersistAndLoad:
    def test_round_trip_with_measured_at(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        fp = measure_lake_footprint(tmp_path)
        meta: dict[str, str] = {}
        stamp = datetime(2026, 7, 4, 10, 30, tzinfo=UTC)
        persist_footprint(meta.__setitem__, fp, measured_at=stamp)
        assert meta[META_MULTIPLIER_OVERALL] == "1.2571"
        assert meta[META_MULTIPLIER_SPOT] == "1.3333"
        assert meta[META_MULTIPLIER_FNO] == "1.2000"
        assert meta[META_SOURCE_BYTES] == "350"
        assert meta[META_DERIVED_BYTES] == "90"
        assert meta[META_MEASURED_AT] == stamp.isoformat()
        assert load_observed_multiplier(meta.get) == Decimal("1.2571")

    def test_empty_segment_persists_no_ratio_key(self) -> None:
        fp = LakeFootprint(
            spot=SegmentFootprint(source_1m_bytes=100, derived_bytes=20),
            fno=SegmentFootprint(source_1m_bytes=0, derived_bytes=0),
        )
        meta: dict[str, str] = {}
        persist_footprint(meta.__setitem__, fp)
        assert META_MULTIPLIER_FNO not in meta  # never a fabricated ratio
        assert META_MULTIPLIER_SPOT in meta
        assert META_MEASURED_AT in meta  # default now(UTC) stamp branch

    def test_load_absent_reads_none(self) -> None:
        assert load_observed_multiplier({}.get) is None

    @pytest.mark.parametrize("bad", ["garbage", "-1", "0"])
    def test_load_invalid_or_nonpositive_reads_none_with_warning(self, bad: str) -> None:
        meta = {META_MULTIPLIER_OVERALL: bad}
        with capture_logs() as logs:
            assert load_observed_multiplier(meta.get) is None
        assert any(e["event"] == "lake.footprint.multiplier_invalid" for e in logs)


class TestRecordLakeFootprint:
    def test_measures_persists_and_logs(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        meta: dict[str, str] = {}
        with capture_logs() as logs:
            fp = record_lake_footprint(tmp_path, meta.__setitem__)
        assert fp is not None and fp.overall.multiplier == Decimal("1.2571")
        assert meta[META_MULTIPLIER_OVERALL] == "1.2571"
        measured = [e for e in logs if e["event"] == "lake.footprint.measured"]
        assert len(measured) == 1
        assert measured[0]["multiplier"] == "1.2571"

    def test_empty_lake_logs_honest_none_multipliers(self, tmp_path: Path) -> None:
        meta: dict[str, str] = {}
        with capture_logs() as logs:
            fp = record_lake_footprint(tmp_path, meta.__setitem__)
        assert fp is not None and fp.overall.multiplier is None
        measured = [e for e in logs if e["event"] == "lake.footprint.measured"]
        assert measured[0]["multiplier"] is None
        assert measured[0]["multiplier_spot"] is None
        assert measured[0]["multiplier_fno"] is None

    def test_best_effort_swallows_and_returns_none(self, tmp_path: Path) -> None:
        def _boom(_k: str, _v: str) -> None:
            raise RuntimeError("sidecar wedged")

        with capture_logs() as logs:
            assert record_lake_footprint(tmp_path, _boom) is None
        assert any(e["event"] == "lake.footprint.record_failed" for e in logs)


class TestDiskGuardEnabled:
    @pytest.mark.parametrize("token", ["off", "0", "false", "no", "OFF", " Off "])
    def test_disabled_tokens(self, token: str) -> None:
        assert disk_guard_enabled({DISK_GUARD_ENV: token}) is False

    @pytest.mark.parametrize("token", ["", "on", "1", "yes"])
    def test_any_other_value_keeps_guard_on(self, token: str) -> None:
        assert disk_guard_enabled({DISK_GUARD_ENV: token}) is True

    def test_default_reads_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DISK_GUARD_ENV, "off")
        assert disk_guard_enabled() is False
        monkeypatch.delenv(DISK_GUARD_ENV)
        assert disk_guard_enabled() is True


class TestEvaluateDiskGuard:
    def test_fallback_multiplier_blocks_short_volume(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        # estimate = 350 * 6.5 = 2275; need = 2275 - 440 = 1835; margin -> int(2018.5) = 2018.
        decision = evaluate_disk_guard(tmp_path, observed_multiplier=None, free_bytes=2017)
        assert decision.blocked is True
        assert decision.multiplier == FALLBACK_MULTIPLIER
        assert decision.multiplier_source == "fallback"
        assert decision.estimated_need_bytes == 1835
        assert decision.source_1m_bytes == 350
        assert decision.derived_bytes == 90
        assert decision.margin == DEFAULT_SAFETY_MARGIN

    def test_fallback_passes_with_enough_free(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        decision = evaluate_disk_guard(tmp_path, observed_multiplier=None, free_bytes=2018)
        assert decision.blocked is False

    def test_observed_multiplier_is_used(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        # estimate = 350 * 2 = 700; need = 700 - 440 = 260; margin -> 286.
        decision = evaluate_disk_guard(tmp_path, observed_multiplier=Decimal("2"), free_bytes=285)
        assert decision.blocked is True
        assert decision.multiplier_source == "observed"
        assert decision.estimated_need_bytes == 260

    def test_zero_need_never_blocks(self, tmp_path: Path) -> None:
        """A fully-materialised lake (on-disk >= estimate) needs nothing."""
        _seed_lake(tmp_path)
        decision = evaluate_disk_guard(tmp_path, observed_multiplier=Decimal("1.0"), free_bytes=0)
        assert decision.blocked is False
        assert decision.estimated_need_bytes == 0

    def test_real_disk_usage_probe_walks_to_existing_parent(self, tmp_path: Path) -> None:
        decision = evaluate_disk_guard(
            tmp_path / "not" / "yet" / "created", observed_multiplier=None
        )
        assert decision.free_bytes > 0  # from shutil.disk_usage of tmp_path
        assert decision.blocked is False  # empty lake: zero source -> zero need

    def test_detail_names_the_numbers_and_override(self, tmp_path: Path) -> None:
        decision = DiskGuardDecision(
            blocked=True,
            free_bytes=2**30,
            estimated_need_bytes=3 * 2**30,
            source_1m_bytes=100,
            derived_bytes=10,
            multiplier=Decimal("6.5"),
            multiplier_source="fallback",
            margin=Decimal("0.10"),
        )
        detail = decision.detail
        assert "est. need ~3.0 GiB" in detail
        assert "x6.5 fallback multiplier" in detail
        assert "+10% margin" in detail
        assert "free 1.0 GiB" in detail
        assert f"{DISK_GUARD_ENV}=off" in detail


class TestMakeDiskGuard:
    def test_disabled_env_returns_none_verdict(self, tmp_path: Path) -> None:
        guard = make_disk_guard(tmp_path, {}.get, env={DISK_GUARD_ENV: "off"})
        assert guard() is None

    def test_no_store_uses_fallback_multiplier(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        guard = make_disk_guard(tmp_path, None, env={})
        decision = guard()
        assert decision is not None
        assert decision.multiplier_source == "fallback"

    def test_observed_meta_feeds_the_guard(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        meta = {META_MULTIPLIER_OVERALL: "2.5"}
        guard = make_disk_guard(tmp_path, meta.get, env={})
        decision = guard()
        assert decision is not None
        assert decision.multiplier == Decimal("2.5")
        assert decision.multiplier_source == "observed"

    def test_fails_open_on_evaluation_error(self, tmp_path: Path) -> None:
        def _boom(_key: str) -> str | None:
            raise RuntimeError("meta store wedged")

        guard = make_disk_guard(tmp_path, _boom, env={})
        with capture_logs() as logs:
            assert guard() is None
        assert any(e["event"] == "pipeline.prebuild.disk_guard_error" for e in logs)
