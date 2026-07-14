"""Whole-tree flag-ON-vs-OFF A/B corpus (DEC-PREBUILD-FRAME-WRITE-001 §3.2).

THE load-bearing byte-identity suite of the frame-direct derived-TF write:
fixture lakes are built through the PRODUCTION writer (plus raw plants for the
legacy/poison shapes the production writer refuses), then
``materialize_lake_timeframes`` runs once flag-OFF and once flag-ON into twin
roots and the resulting ``bars/`` trees must be BYTE-IDENTICAL (same file set,
same per-file sha256) with equal outcomes — or the SAME exception class on
both arms (V3 C8/C16):

1.  clean snapped spot lake (cold build, burn-in exercised);
2.  LEGACY OFF-GRID floats raw-planted in the 1m source (the V3 §9.2 class —
    the unconditional-snap ruling's reason to exist);
3.  F&O intraday with null-OI buckets + carry-last gaps;
4.  union-never-delete: a pre-existing derived partition holding rows ABSENT
    from the fresh frame is KEPT (the provable no-op), and a GROWN source
    degrades ``merge_delta`` into the legacy union;
5.  a derived partition with PHYSICAL duplicate timestamps (degrade
    ``merge_stored_dups``; the legacy dedup self-heal fires identically);
7.  Muhurat-session day in the 1m source + the daily D−1 UTC month straddle
    (``include_daily`` — the daily writers are legacy-lane BY DESIGN; the
    whole-tree compare covers the interaction);
8.  exact ``.005`` half-up boundary prices + max-realistic magnitudes;
9.  poison rows → the SAME exception class both arms (negative bucket volume
    at the write boundary; high<low at the price-sanity read boundary);
10. WARM re-run over an already-materialised tree: ZERO renders / ZERO fsyncs
    / ZERO replaces on BOTH arms (op-count spies — the C4 mtime-churn guard);
11. a hand-enriched (overlay-column) derived partition → degrade
    ``merge_overlay``; the legacy latest-wins overlay STRIP reproduced;
12. a FORCED multi-chunk frame through ``append_frame`` → the unconditional
    ``rechunk`` pins the byte law (graft G-C);
13. a SEEDED stats-manifest shard → hook-parity (same upsert count + same
    rows/raw_hash arguments) on both arms (graft G-D);
plus the DEFAULTS negative pin: flag UNSET in PR-1 ⇒ zero fast-lane code
executes (spies on ``append_frame``/``write_frame``) and the tree equals the
explicit flag-``0`` tree. (Corpus case 6 — the empty-frame no-write law — is
unit-pinned in ``test_writer_frame_write.py``: the sink early-return.)

Whitebox spies on writer privates — ``reportPrivateUsage`` silenced per the
DEC-P0-017 convention.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data import frame_write_gate as gate
from brutex.data.frame_write_gate import PREBUILD_FRAME_WRITE_ENV
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import (
    MaterializeResult,
    PrebuildKind,
    materialize_lake_timeframes,
)

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl

_EXCHANGE = "NSE"
#: 09:15 IST on a regular NSE session.
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)

_LAKE_SCHEMA: dict[str, Any] = {
    "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "open_interest": pl.Int64,
}


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PREBUILD_FRAME_WRITE_ENV, raising=False)
    gate.reset_gate_for_tests()
    gate.reset_observability_memo_for_tests()


def _seed_bars(
    root: Path,
    symbol: str,
    *,
    segment: str = "INDEX",
    start: datetime = _SESSION_OPEN_UTC,
    n: int = 120,
    base: float = 100.0,
    oi: int | None = 0,
    null_oi_at: frozenset[int] = frozenset(),
) -> None:
    """Seed *n* 1-minute bars through the PRODUCTION writer."""
    bars = [
        CandleBar(
            timestamp=start + timedelta(minutes=i),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + 10 + i)),
            low=Decimal(str(base - 5 + i)),
            close=Decimal(str(base + 2 + i)),
            volume=10 + i,
            open_interest=None if i in null_oi_at else oi if oi is None else oi + i,
        )
        for i in range(n)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=root))
    sink.append(exchange=_EXCHANGE, segment=segment, symbol=symbol, interval="1minute", bars=bars)


def _plant_raw_1m(
    root: Path,
    symbol: str,
    rows: dict[str, list[Any]],
    *,
    segment: str = "INDEX",
    year: int = 2021,
    month: int = 1,
) -> None:
    """Raw-plant a 1-minute partition (legacy / poison shapes the writer refuses)."""
    dest = (
        root
        / "bars"
        / _EXCHANGE
        / segment
        / symbol
        / "1minute"
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=_LAKE_SCHEMA).write_parquet(dest)


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "bars").rglob("*.parquet"))
    }


def _derive(
    root: Path,
    flag: str | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str,
    segment: str = "INDEX",
    kind: PrebuildKind = PrebuildKind.SPOT,
    tfs: tuple[str, ...] = ("5m", "15m", "60m"),
    include_daily: bool = False,
    daily_before: date | None = None,
    burn_in: bool = False,
) -> MaterializeResult:
    """One arm of the A/B: reset the gate, set the flag, run the REAL derive."""
    if flag is None:
        monkeypatch.delenv(PREBUILD_FRAME_WRITE_ENV, raising=False)
    else:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, flag)
    if burn_in:
        gate.reset_gate_for_tests()  # full budgets — the burn-in path runs
    else:
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
    gate.reset_observability_memo_for_tests()
    return materialize_lake_timeframes(
        lake_root=root,
        exchange=_EXCHANGE,
        segment=segment,
        symbol=symbol,
        kind=kind,
        tfs=tfs,
        include_daily=include_daily,
        daily_before=daily_before,
        incremental=False,
    )


def _assert_ab_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: Any,
    *,
    burn_in: bool = False,
    **derive_kwargs: Any,
) -> tuple[Path, Path]:
    """Seed twin roots, derive OFF vs ON, assert whole-tree byte identity."""
    root_off, root_on = tmp_path / "off", tmp_path / "on"
    seed(root_off)
    seed(root_on)
    result_off = _derive(root_off, "0", monkeypatch, burn_in=burn_in, **derive_kwargs)
    result_on = _derive(root_on, "1", monkeypatch, burn_in=burn_in, **derive_kwargs)
    assert result_off.written == result_on.written
    assert result_off.skipped == result_on.skipped
    tree_off, tree_on = _tree(root_off), _tree(root_on)
    assert set(tree_off) == set(tree_on), set(tree_off) ^ set(tree_on)
    diverged = [rel for rel in tree_off if tree_off[rel] != tree_on[rel]]
    assert not diverged, diverged
    return root_off, root_on


# ── 1. clean snapped spot lake (cold; burn-in AND steady) ─────────────────


@pytest.mark.parametrize("burn_in", [True, False], ids=["burn_in", "steady"])
def test_case1_clean_spot_cold_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, burn_in: bool
) -> None:
    _assert_ab_identical(
        tmp_path,
        monkeypatch,
        lambda root: _seed_bars(root, "NIFTY"),
        symbol="NIFTY",
        burn_in=burn_in,
    )


# ── 2. LEGACY OFF-GRID floats in the 1m source (the refuted-idempotence class) ──


def test_case2_off_grid_legacy_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The V3 §9.2 boundary values — the unconditional snap must re-snap both arms."""

    def seed(root: Path) -> None:
        n = 20
        _plant_raw_1m(
            root,
            "NIFTY",
            {
                "timestamp": [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(n)],
                "open": [14020.7999 + i for i in range(n)],
                "high": [14031.0001 + i for i in range(n)],
                "low": [14019.5 + i for i in range(n)],
                "close": [14020.005 + i for i in range(n)],
                "volume": [10 + i for i in range(n)],
                "open_interest": [0] * n,
            },
        )

    _assert_ab_identical(tmp_path, monkeypatch, seed, symbol="NIFTY")


# ── 3. F&O intraday with null-OI buckets + carry-last gaps ─────────────────


def test_case3_fno_null_oi_carry_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_ab_identical(
        tmp_path,
        monkeypatch,
        lambda root: _seed_bars(
            root,
            "NSE-NIFTY-28Jan21-FUT",
            segment="FNO",
            oi=500,
            null_oi_at=frozenset({0, 1, 2, 7, 40}),
        ),
        symbol="NSE-NIFTY-28Jan21-FUT",
        segment="FNO",
        kind=PrebuildKind.FUTURES,
    )


# ── 4. union-never-delete: stored-extra no-op + grown-source merge_delta ───


def test_case4_union_never_delete_and_merge_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_off, root_on = tmp_path / "off", tmp_path / "on"
    for root in (root_off, root_on):
        _seed_bars(root, "NIFTY", n=120)
    # First derive (both arms OFF — a shared legacy baseline).
    for root in (root_off, root_on):
        _derive(root, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))

    # 4a: hand-UNION extra rows into the derived partition (rows ABSENT from
    # the fresh frame — the recomputed frame must never delete them).
    extra = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=200 + 5 * i),
            open=Decimal("500.00"),
            high=Decimal("510.00"),
            low=Decimal("495.00"),
            close=Decimal("505.00"),
            volume=42,
            open_interest=0,
        )
        for i in range(3)
    ]
    for root in (root_off, root_on):
        ParquetWriter(lake_root=root).write(
            exchange=_EXCHANGE,
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            bars=extra,
            merge_mode="latest-wins",
        )
    # Re-derive: the incoming frame ⊂ stored → the fast lane's provable NO-OP;
    # the extra rows are KEPT on both arms, trees identical.
    result_off = _derive(root_off, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
    result_on = _derive(root_on, "1", monkeypatch, symbol="NIFTY", tfs=("5m",))
    assert result_off.written == result_on.written
    assert _tree(root_off) == _tree(root_on)
    stored = pl.read_parquet(
        root_on / "bars" / _EXCHANGE / "INDEX" / "NIFTY" / "5minute" / "2021" / "01.parquet"
    )
    assert stored.height == 24 + 3  # union-never-delete: the extras survived

    # 4b: GROW the 1m source → the current-month bucket set changes → the fast
    # lane degrades merge_delta and the legacy union runs on both arms.
    for root in (root_off, root_on):
        _seed_bars(root, "NIFTY", start=_SESSION_OPEN_UTC + timedelta(minutes=120), n=60)
    _derive(root_off, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
    _derive(root_on, "1", monkeypatch, symbol="NIFTY", tfs=("5m",))
    assert _tree(root_off) == _tree(root_on)


# ── 5. stored PHYSICAL duplicate timestamps (pending dedup self-heal) ──────


def test_case5_stored_dup_heal_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_off, root_on = tmp_path / "off", tmp_path / "on"
    for root in (root_off, root_on):
        _seed_bars(root, "NIFTY", n=60)
        _derive(root, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
        dest = root / "bars" / _EXCHANGE / "INDEX" / "NIFTY" / "5minute" / "2021" / "01.parquet"
        stored = pl.read_parquet(dest)
        # Poison: duplicate the first physical row (a pre-DEC-LAKE-WRITE-UNIQUE
        # shape) — the production writer never lands this; raw re-write.
        pl.concat([stored.slice(0, 1), stored]).write_parquet(dest)

    _derive(root_off, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
    _derive(root_on, "1", monkeypatch, symbol="NIFTY", tfs=("5m",))
    assert _tree(root_off) == _tree(root_on)
    healed = pl.read_parquet(
        root_on / "bars" / _EXCHANGE / "INDEX" / "NIFTY" / "5minute" / "2021" / "01.parquet"
    )
    assert healed.get_column("timestamp").n_unique() == healed.height  # dedup-healed


# ── 7. Muhurat session + the daily D−1 UTC month straddle ─────────────────


def test_case7_muhurat_and_daily_d_minus_1_straddle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(root: Path) -> None:
        # A regular session on the 1st of a month: its midnight-IST daily bar
        # (18:30 UTC of D−1) lands in the PREVIOUS UTC month partition.
        _seed_bars(root, "NIFTY", start=datetime(2021, 2, 1, 3, 45, tzinfo=UTC), n=60)
        # The 2021 Diwali Muhurat evening session (2021-11-04, ~18:15 IST).
        _seed_bars(root, "NIFTY", start=datetime(2021, 11, 4, 12, 45, tzinfo=UTC), n=60, base=300.0)

    _assert_ab_identical(
        tmp_path,
        monkeypatch,
        seed,
        symbol="NIFTY",
        include_daily=True,
        daily_before=date(2021, 11, 6),
    )


# ── 8. half-up boundaries + max-realistic magnitudes ───────────────────────


def test_case8_half_up_boundaries_and_magnitudes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(root: Path) -> None:
        n = 30
        _plant_raw_1m(
            root,
            "NIFTY",
            {
                "timestamp": [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(n)],
                "open": [100.005 + i for i in range(n)],
                "high": [999999.995] * n,
                "low": [0.005] * n,
                "close": [2.675 + i for i in range(n)],
                "volume": [1] * n,
                "open_interest": [0] * n,
            },
        )

    _assert_ab_identical(tmp_path, monkeypatch, seed, symbol="NIFTY")


# ── 9. poison rows → the SAME exception class both arms ────────────────────


def test_case9_negative_bucket_volume_same_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A negative BUCKET volume passes price_sanity (prices only) and fails at
    bar construction — the G3 degrade reproduces the exact legacy class."""

    def seed(root: Path) -> None:
        n = 10
        volumes = [10] * n
        volumes[3] = -10_000  # dominates its bucket → a negative 5m sum
        _plant_raw_1m(
            root,
            "NIFTY",
            {
                "timestamp": [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(n)],
                "open": [100.0 + i for i in range(n)],
                "high": [110.0 + i for i in range(n)],
                "low": [95.0 + i for i in range(n)],
                "close": [102.0 + i for i in range(n)],
                "volume": volumes,
                "open_interest": [0] * n,
            },
        )

    root_off, root_on = tmp_path / "off", tmp_path / "on"
    seed(root_off)
    seed(root_on)
    with pytest.raises(ValueError, match="volume") as off_exc:
        _derive(root_off, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
    with pytest.raises(ValueError, match="volume") as on_exc:
        _derive(root_on, "1", monkeypatch, symbol="NIFTY", tfs=("5m",))
    assert type(off_exc.value) is type(on_exc.value)


def test_case9_price_insane_source_same_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """high<low is refused UPSTREAM (price_sanity) identically on both arms."""
    from brutex.data.errors import ResampleInputError

    def seed(root: Path) -> None:
        _plant_raw_1m(
            root,
            "NIFTY",
            {
                "timestamp": [_SESSION_OPEN_UTC],
                "open": [100.0],
                "high": [90.0],  # high < low — the corrupt class
                "low": [95.0],
                "close": [96.0],
                "volume": [10],
                "open_interest": [0],
            },
        )

    for flag, sub in (("0", "off"), ("1", "on")):
        root = tmp_path / sub
        seed(root)
        with pytest.raises(ResampleInputError):
            _derive(root, flag, monkeypatch, symbol="NIFTY", tfs=("5m",))


# ── 10. WARM re-run: zero renders / fsyncs / replaces on BOTH arms ─────────


def test_case10_warm_rerun_zero_renders_zero_fsyncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_off, root_on = tmp_path / "off", tmp_path / "on"
    for root in (root_off, root_on):
        _seed_bars(root, "NIFTY", n=120)
    _derive(root_off, "0", monkeypatch, symbol="NIFTY")
    _derive(root_on, "1", monkeypatch, symbol="NIFTY")
    tree_before = {"off": _tree(root_off), "on": _tree(root_on)}

    original_render = ParquetWriter._render_frame_bytes
    original_fsync = os.fsync
    original_replace = os.replace
    for flag, root, arm in (("0", root_off, "off"), ("1", root_on, "on")):
        ops = {"renders": 0, "fsyncs": 0, "replaces": 0}

        def counting_render(df: pl.DataFrame, _ops: dict[str, int] = ops) -> bytes:
            _ops["renders"] += 1
            return original_render(df)

        def counting_fsync(fd: int, _ops: dict[str, int] = ops) -> None:
            _ops["fsyncs"] += 1
            original_fsync(fd)

        def counting_replace(src: Any, dst: Any, _ops: dict[str, int] = ops, **kw: Any) -> None:
            _ops["replaces"] += 1
            original_replace(src, dst, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ParquetWriter, "_render_frame_bytes", staticmethod(counting_render))
            mp.setattr(os, "fsync", counting_fsync)
            mp.setattr(os, "replace", counting_replace)
            result = _derive(root, flag, monkeypatch, symbol="NIFTY")
        assert ops == {"renders": 0, "fsyncs": 0, "replaces": 0}, f"{arm}: warm re-run wrote"
        assert result.written  # the derive genuinely ran (bars counted)
        assert _tree(root) == tree_before[arm]


# ── 11. hand-enriched overlay partition → legacy strip on BOTH arms ────────


def test_case11_overlay_strip_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_off, root_on = tmp_path / "off", tmp_path / "on"
    for root in (root_off, root_on):
        _seed_bars(root, "NIFTY", n=60)
        _derive(root, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
        dest = root / "bars" / _EXCHANGE / "INDEX" / "NIFTY" / "5minute" / "2021" / "01.parquet"
        enriched = pl.read_parquet(dest).with_columns(
            pl.lit(0.42).alias("iv"), pl.lit(0.5).alias("delta")
        )
        enriched.write_parquet(dest)

    _derive(root_off, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
    _derive(root_on, "1", monkeypatch, symbol="NIFTY", tfs=("5m",))
    assert _tree(root_off) == _tree(root_on)
    stripped = pl.read_parquet(
        root_on / "bars" / _EXCHANGE / "INDEX" / "NIFTY" / "5minute" / "2021" / "01.parquet"
    )
    # Legacy latest-wins re-derive STRIPS a hand-planted overlay (V3 §4) —
    # reproduced on the ON arm via the merge_overlay degrade.
    assert "iv" not in stripped.columns


# ── 12. FORCED multi-chunk frame through append_frame (graft G-C) ──────────


def test_case12_multi_chunk_frame_byte_law(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "1")
    gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)

    def frame_part(offset: int, n: int) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "ts_utc": [
                    _SESSION_OPEN_UTC + timedelta(minutes=5 * (offset + i)) for i in range(n)
                ],
                "open": [100.0 + offset + i for i in range(n)],
                "high": [110.0 + offset + i for i in range(n)],
                "low": [95.0 + offset + i for i in range(n)],
                "close": [102.0 + offset + i for i in range(n)],
                "volume": [10 + i for i in range(n)],
            },
            schema={
                "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
            },
        )

    multi = pl.concat([frame_part(0, 3), frame_part(3, 5)], rechunk=False)
    assert multi.n_chunks() > 1
    single = multi.rechunk()

    root_multi, root_single = tmp_path / "multi", tmp_path / "single"
    for root, frame in ((root_multi, multi), (root_single, single)):
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=root, partition_log_level="debug"))
        sink.append_frame(
            exchange=_EXCHANGE,
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            frame=frame,
            merge_mode="latest-wins",
        )
    assert _tree(root_multi) == _tree(root_single)


# ── 13. SEEDED stats-manifest shard → hook parity (graft G-D) ──────────────


def test_case13_seeded_stats_shard_hook_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brutex.data.lake_stats_manifest as lsm
    import brutex.data.parquet_writer as pw_module

    root_off, root_on = tmp_path / "off", tmp_path / "on"
    for root in (root_off, root_on):
        _seed_bars(root, "NIFTY", n=60)
        _derive(root, "0", monkeypatch, symbol="NIFTY", tfs=("5m",))
        # Seed the derived dataset's shard (a "/db-viewed" dataset) from the
        # real on-disk file — the read-side seeding law.
        dest = root / "bars" / _EXCHANGE / "INDEX" / "NIFTY" / "5minute" / "2021" / "01.parquet"
        frame = pl.read_parquet(dest)
        stat = os.stat(dest)
        row = lsm.StatsFileRow(
            rel="2021/01.parquet",
            rows=frame.height,
            min_ts=frame.get_column("timestamp").min(),  # type: ignore[arg-type]
            max_ts=frame.get_column("timestamp").max(),  # type: ignore[arg-type]
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            raw_hash=hashlib.sha256(dest.read_bytes()).hexdigest(),
        )
        key = {
            "exchange": _EXCHANGE,
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "5minute",
        }
        assert lsm.seed_dataset_stats(root, kind=lsm.KIND_BARS, key=key, files=[row])
        # GROW the source so the re-derive genuinely rewrites (merge_delta).
        _seed_bars(root, "NIFTY", start=_SESSION_OPEN_UTC + timedelta(minutes=60), n=30)

    upserts: dict[str, list[tuple[int, str]]] = {"off": [], "on": []}
    original_record = pw_module.record_partition_stats

    for flag, root, arm in (("0", root_off, "off"), ("1", root_on, "on")):

        def recording(
            lake_root: Path,
            *,
            kind: str,
            key: Any,
            dest: Path,
            rows: int,
            min_ts: Any,
            max_ts: Any,
            raw_hash: str,
            stat: Any,
            _arm: str = arm,
        ) -> None:
            upserts[_arm].append((rows, raw_hash))
            original_record(
                lake_root,
                kind=kind,
                key=key,
                dest=dest,
                rows=rows,
                min_ts=min_ts,
                max_ts=max_ts,
                raw_hash=raw_hash,
                stat=stat,
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pw_module, "record_partition_stats", recording)
            _derive(root, flag, monkeypatch, symbol="NIFTY", tfs=("5m",))

    assert _tree(root_off) == _tree(root_on)
    # Hook parity: the SAME upsert count with the SAME rows + raw_hash args.
    assert upserts["off"] == upserts["on"]
    assert len(upserts["off"]) >= 1  # the tracked shard genuinely re-stamped


# ── DEFAULTS negative pin: PR-1 unset flag ⇒ zero fast-lane code ───────────


def test_defaults_negative_pin_zero_fast_lane_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag UNSET in PR-1 ⇒ append_frame/write_frame NEVER execute and the
    tree equals the explicit flag-'0' tree (provably inert at defaults)."""
    calls = {"append_frame": 0, "write_frame": 0}
    original_af = ParquetBarSink.append_frame
    original_wf = ParquetWriter.write_frame

    def spy_af(self: ParquetBarSink, **kwargs: Any) -> Any:
        calls["append_frame"] += 1
        return original_af(self, **kwargs)

    def spy_wf(self: ParquetWriter, **kwargs: Any) -> Any:
        calls["write_frame"] += 1
        return original_wf(self, **kwargs)

    monkeypatch.setattr(ParquetBarSink, "append_frame", spy_af)
    monkeypatch.setattr(ParquetWriter, "write_frame", spy_wf)

    root_default, root_off = tmp_path / "default", tmp_path / "off"
    for root in (root_default, root_off):
        _seed_bars(root, "NIFTY", n=60)
    _derive(root_default, None, monkeypatch, symbol="NIFTY")  # flag UNSET
    assert calls == {"append_frame": 0, "write_frame": 0}
    _derive(root_off, "0", monkeypatch, symbol="NIFTY")
    assert calls == {"append_frame": 0, "write_frame": 0}
    assert _tree(root_default) == _tree(root_off)
