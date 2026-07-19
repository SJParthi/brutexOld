"""Golden — the ADR-0012 futures launch identity chain, frozen end-to-end.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001: a mini 2-contract Parquet lake (written
with the REAL ``ParquetBarSink``) is resolved by the REAL
``resolve_futures_series``, stitched by the REAL ``load_sweep_slice``
IST-date ownership cut, snapshot-hashed by the REAL launch digest
(``stage3_wire.data_snapshot_hash``) and swept by the REAL
``run_persisted_sweep`` under the series alias ``NSE-NIFTY-FUT`` — freezing
the **addressing → clip → snapshot → identity** chain with three literals:

* the resolver's ``roll_schedule`` (vendor-token day-ordinals);
* the full-content ``data_snapshot_hash`` of the stitched slice (any
  ownership-cut / stitch-order drift re-keys it — Golden Rule #4);
* one pinned ``mask_run_id`` + the end-to-end ``store_content_hash``.

Mirrors the LOGICAL-first philosophy of
``tests/golden/test_engine_sweep_run_golden.py``: the determinism + summary
assertions are the version-robust contract; the literal store-content-hash
pin is polars/zstd-build sensitive (same caveat that golden documents) — a
mismatch there with the determinism assertions still green means a dependency
bump, not a logic regression.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.catalog import Catalog
from brutex.data.futures_series import resolve_futures_series
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.results_store import ResultsStore
from brutex.engine.stage3_wire import data_snapshot_hash
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    canonical_trade_params,
    mask_run_id,
    run_persisted_sweep,
)
from brutex.feed.series_load import load_sweep_slice
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

_IST = timezone(timedelta(hours=5, minutes=30))

_ALIAS = "NSE-NIFTY-FUT"
_JUN_CONTRACT = "NSE-NIFTY-26Jun25-FUT"
_JUL_CONTRACT = "NSE-NIFTY-31Jul25-FUT"
_EXPIRY_JUN = date(2025, 6, 26)
_EXPIRY_JUL = date(2025, 7, 31)
_WINDOW_START = date(2025, 6, 24)
_WINDOW_END = date(2025, 6, 30)
_BARS_PER_DAY = 120

#: The frozen resolver output for this lake + window (vendor-token ordinals).
_EXPECTED_ROLL_SCHEDULE = (_EXPIRY_JUN.toordinal(), _EXPIRY_JUL.toordinal())

#: The frozen full-content bxsnap2 digest of the stitched 600-bar slice —
#: any ownership-cut / stitch-order / bar-content drift re-keys it.
# RE-LOCKED for DEC-FILL-WORST-CASE-ONLY-001 (2026-07-15): the fixture was
# re-crafted to the rankable gap-up shape (see _day_bars), so the stitched
# slice's bytes — and hence the snapshot hash — moved by design.
_EXPECTED_SNAPSHOT_HASH = "1a8f36dd8d04452a77a49ba878dd2c473bc6d5418cdabf732348661704c8434e"

#: The frozen run identity of mask 0b101 under the futures context (the
#: params carry the recorded ``roll_schedule`` — the aliasing kill-shot's
#: identity surface).
# RE-LOCKED for DEC-FILL-WORST-CASE-ONLY-001 (2026-07-15): the params layout
# now carries the UNCONDITIONAL ``fill_mode: worst_case`` token AND the
# snapshot moved with the re-crafted fixture — the run identity moved by
# design (the precise-era digest f6a08f4c…b1b4c9 is permanently unreachable).
_EXPECTED_RUN_ID = "2aa8c684565a5e250a86d59b8161e8bfed3e1e94fbc0f8a91fce128c1ce020ca"

#: The frozen end-to-end store content hash (polars/zstd-build sensitive —
#: see the module docstring).
#: Re-locked for DEC-VIX-STAMP-WIRE-001 (#1274): the persisted schema gained
#: the three appended nullable REFERENCE-ONLY ``vix_mean``/``vix_min``/
#: ``vix_max`` columns (all null on this UNSTAMPED futures fixture — the
#: honest unknown), so the deterministic byte stream moved by design. The
#: delta was PROVEN VIX-only against the pre-fold store (old file sha256 ==
#: the prior literal ``82211c97…``): dropping the trio leaves the 4×25 frame
#: byte-for-byte frame-equal — every non-VIX row value, every run_id, the
#: roll_schedule / snapshot-hash / run-id literals above all unchanged.
# RE-LOCKED for DEC-FILL-WORST-CASE-ONLY-001 (2026-07-15): worst-case fills
# over the re-crafted fixture — regenerated on THIS tree, two-run
# byte-determinism pinned in the literal test below.
# RE-LOCKED for the W2 metrics-depth fold (MIGRATION.md #1974–#1976, the
# #1976 census fold of main ace41c7e0 — regenerated 2026-07-19 on the
# folded tree with a FRESH hand-audit under worst-case fill semantics; the
# house law: goldens are re-derived, never merged textually; the pre-fold
# sl_hit_rate/recency audit values were PRE-worst-case HISTORY and are
# superseded by the figures below). AUDITED DELTA CLASS — schema-additive
# ONLY, proven against a live main-baseline worktree at ace41c7e0 (which
# reproduces its literal `8670c7f6…74a0` exactly): the SAME 3 rows with
# byte-stable run_ids, the SAME 28-column prefix in the SAME order with
# EVERY old column value byte-equal, plus EXACTLY the lane's 8
# appended-LAST nullable columns (the growth trio + sl_hit_rate + the
# recency four). WORST-CASE HAND-AUDIT of the mask bit-0 row (142 trades =
# 122 TP wins × +50 → gross_profit 6_100, + 20 SL stop-outs → gross_loss
# 2_210 across the two bearish-bar depths, avg −110.5; net +3_890):
# loss_rate = 20/142 = 0.14084507042253522; every loss is an SL fill under
# worst-case → sl_hit_rate = 20/142 (== loss_rate — all wins are TP);
# kelly = p − q/b with p = 122/142, b = 50/110.5 → 0.5478873239436619 and
# g(f*/2) = 0.055308117989589976; the 5-session Jun-2025 stitched slice
# sits wholly inside the 24-month trailing window AND decay bucket 0 →
# trailing_net_pnl_paisa == decay_weighted_net_paisa == net (+3_890) and
# trailing sharpe/pf equal the full-history columns.
_EXPECTED_STORE_HASH = "1022bebf4e372f2ca8291a90bdab9f32e189822778447382eb01aa1dce737728"


def _day_bars(day: date, base: float, n: int = _BARS_PER_DAY) -> list[CandleBar]:
    """The worst-case-rankable gap-up intraday shape, anchored on *day*.

    RE-CRAFTED for DEC-FILL-WORST-CASE-ONLY-001 (the old wide-range drift
    SL'd every worst-case entry on its own bar — nothing persisted): narrow
    gap-up bars (open = prior close + 3.00, range 0.50 < the SL 0.80; the
    next gap-up's low clears the TP level → +50 wins), a shallow bearish bar
    each 7-bar cycle, and a DEEP bearish bar (range 0.85) every 4th cycle
    whose stop-out keeps profit_factor finite.
    """
    out: list[CandleBar] = []
    price = base
    for m in range(n):
        k = m % 7
        if k == 3:  # shallow bearish bar
            o = price
            c, h, lo = o - 0.3, o + 0.1, o - 0.4
        elif k == 4 and (m // 7) % 4 == 0:  # deep bearish loss carrier
            o = price
            c, h, lo = o - 0.65, o + 0.1, o - 0.75
        else:  # narrow gap-up win bar
            o = price + 3.0
            c, h, lo = o + 0.3, o + 0.4, o - 0.1
        ts = datetime(day.year, day.month, day.day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
        out.append(
            CandleBar(
                timestamp=ts.astimezone(UTC),
                open=Decimal(str(round(o, 2))),
                high=Decimal(str(round(h, 2))),
                low=Decimal(str(round(lo, 2))),
                close=Decimal(str(round(c, 2))),
                volume=100,
                open_interest=None,
            )
        )
        price = c
    return out


def _write_lake(root: Path) -> None:
    """The 2-contract mini-lake: Jun owns 24-26 Jun; Jul overlaps 25-26 (CUT) + owns 27/30."""
    writer = ParquetWriter(lake_root=root)
    sink = ParquetBarSink(writer=writer)
    jun_bars = [
        b
        for day in (date(2025, 6, 24), date(2025, 6, 25), _EXPIRY_JUN)
        for b in _day_bars(day, 100.0)
    ]
    jul_bars = [
        # The overlap days (25-26 Jun) exist in the Jul contract's partition at
        # a basis-gapped price — the loader's IST-date cut MUST drop them.
        b
        for day in (date(2025, 6, 25), _EXPIRY_JUN)
        for b in _day_bars(day, 140.0, n=30)
    ] + [b for day in (date(2025, 6, 27), date(2025, 6, 30)) for b in _day_bars(day, 140.0)]
    sink.append(
        exchange="NSE", segment="FNO", symbol=_JUN_CONTRACT, interval="1minute", bars=jun_bars
    )
    sink.append(
        exchange="NSE", segment="FNO", symbol=_JUL_CONTRACT, interval="1minute", bars=jul_bars
    )


@pytest.fixture(scope="module")
def stitched(tmp_path_factory: pytest.TempPathFactory) -> tuple[list[CandleBar], tuple[int, ...]]:
    """(stitched slice, roll_schedule) loaded ONCE through the real chain."""
    root = tmp_path_factory.mktemp("futures_golden_lake")
    _write_lake(root)
    res = resolve_futures_series(root, start=_WINDOW_START, end=_WINDOW_END)
    with Catalog.open(root / "catalog.duckdb") as cat:
        bars = load_sweep_slice(
            cat=cat,
            lake_root=root,
            exchange="NSE",
            segment="FNO",
            symbol=_ALIAS,
            interval="1minute",
            start=datetime(2025, 6, 24, 0, 0, tzinfo=_IST),
            end=datetime(2025, 6, 30, 23, 59, tzinfo=_IST),
            resolution=res,
        )
    return bars, res.roll_schedule


def _snapshot(bars: list[CandleBar]) -> str:
    return data_snapshot_hash(
        exchange="NSE",
        segment="FNO",
        start=_WINDOW_START.isoformat(),
        end=_WINDOW_END.isoformat(),
        slices={(_ALIAS, "1minute"): bars},
    )


def _config(bars: list[CandleBar], schedule: tuple[int, ...]) -> SweepRunConfig:
    return SweepRunConfig(
        instrument=_ALIAS,
        tf="1m",
        direction=Side.BUY,
        mode="A",
        condition_bits=[0, 1, 2, 3, 4],
        k=1,
        data_snapshot_hash=_snapshot(bars),
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        tf_minutes=1,
        sl_paisa=80,
        tp_paisa=50,
        roll_schedule=schedule,
    )


def _run(tmp_path: Path, tag: str, bars: list[CandleBar], cfg: SweepRunConfig) -> SweepRunSummary:
    store = ResultsStore(path=tmp_path / f"{tag}.parquet")
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
        return run_persisted_sweep(bars, cfg, store=store, dedup=dedup, audit=audit)


# ────── the frozen chain ──────


def test_golden_roll_schedule_is_vendor_token_ordinals(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    _, schedule = stitched
    assert schedule == _EXPECTED_ROLL_SCHEDULE


def test_golden_stitched_slice_shape(stitched: tuple[list[CandleBar], tuple[int, ...]]) -> None:
    bars, _ = stitched
    # 3 Jun-owned days + 2 Jul-owned days × 120 bars — the 60 overlap bars CUT.
    assert len(bars) == 5 * _BARS_PER_DAY
    assert all(a.timestamp < b.timestamp for a, b in itertools.pairwise(bars))
    # Expiry-day ownership: 26 Jun bars carry the Jun contract's price plane
    # (base 100 — the day's FIRST gap-up bar opens at 103.00), never the Jul
    # contract's basis-gapped plane (base 140 — its first bar would open at
    # 143.00). The gap-up fixture drifts ~+400 intraday so the whole-day
    # ranges overlap between planes; the first-bar open is the clean
    # discriminator.
    expiry_day = [b for b in bars if b.timestamp.astimezone(_IST).date() == _EXPIRY_JUN]
    assert len(expiry_day) == _BARS_PER_DAY
    assert expiry_day[0].open == Decimal("103.00")
    assert expiry_day[0].open < Decimal("139")  # the Jun plane, never Jul's


def test_golden_snapshot_hash_literal(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    bars, _ = stitched
    assert _snapshot(bars) == _EXPECTED_SNAPSHOT_HASH


def test_golden_run_id_literal(stitched: tuple[list[CandleBar], tuple[int, ...]]) -> None:
    bars, schedule = stitched
    params = canonical_trade_params(
        sl_paisa=80,
        tp_paisa=50,
        prior_n=3,
        proximity=None,
        start_equity_paisa=10_000_000,
        roll_schedule=schedule,
    )
    rid = mask_run_id(
        mask=0b101,
        direction="bull",
        instrument=_ALIAS,
        tf="1m",
        mode="A",
        params=params,
        data_snapshot_hash=_snapshot(bars),
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )
    assert rid == _EXPECTED_RUN_ID


def test_golden_store_hash_literal_and_determinism(
    stitched: tuple[list[CandleBar], tuple[int, ...]], tmp_path: Path
) -> None:
    bars, schedule = stitched
    cfg = _config(bars, schedule)
    a = _run(tmp_path, "a", bars, cfg)
    b = _run(tmp_path, "b", bars, cfg)
    assert a.enumerated == 5
    assert a.persisted > 0  # the futures sweep genuinely ranked finalists
    assert a.store_content_hash == b.store_content_hash
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()
    assert a.store_content_hash == _EXPECTED_STORE_HASH


def test_golden_one_extra_bar_rekeys_the_identity(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    """F12 at the golden grain: a one-bar lake difference moves the snapshot."""
    bars, _ = stitched
    extra = _day_bars(date(2025, 6, 30), 140.0, n=_BARS_PER_DAY + 1)[-1]
    assert _snapshot([*bars, extra]) != _EXPECTED_SNAPSHOT_HASH
