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
# RE-LOCKED for DEC-FILL-EXEC-GRID-1M-001 (2026-07-18): the ONE preimage
# change is that ``canonical_trade_params`` now UNCONDITIONALLY emits the
# additive ``execution_grid: "1m_next_bar"`` key into the canonical params
# dict (the anti-aliasing token vs PR-A run_ids), so the blake3 run-identity
# digest moved by design. Every OTHER identity input is byte-unchanged —
# the snapshot-hash literal above still passes UNMODIFIED, and
# mask/direction/instrument/tf/mode/indicator_version/code_commit_sha are
# the same literals (the PR-A digest 2aa8c684…020ca is permanently
# unreachable).
_EXPECTED_RUN_ID = "31b6fd548d03df1fd591b4550d7844e01e26e124e7586768935eb26829457aa6"

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
# RE-LOCKED for DEC-FILL-EXEC-GRID-1M-001 × TRAIN-B FOLD: the persisted store
# is the UNION of the member's exec-grid identity and the woven additive
# columns. (1) Every row's ``run_id`` re-keys on the additive
# ``execution_grid: "1m_next_bar"`` params token — the same preimage change as
# ``_EXPECTED_RUN_ID`` above; the TRADES are unchanged: a per-mask walker probe
# over this exact stitched slice (masks 1/2/4/8/16, SL 80 / TP 50) measured
# ``exits_deferred == 0`` on every mask (no entry 1m bar here touches a level,
# so the §2.4 same-bar deferral is inert and the B3 carve-out never applies) —
# the 142-trade worst-case shape (122 TP × +50 + 20 SL stop-outs, net +3_890)
# survives (loss_rate = 20/142; every loss is an SL fill under worst-case →
# sl_hit_rate == loss_rate). (2) The ResultRow schema carries the woven W2
# metrics-depth family (8 appended-LAST nullable columns: the growth trio +
# sl_hit_rate + the recency four, MIGRATION.md #1974–#1976) PLUS the LANE-W1
# selection-provenance pair (`pareto_member` Boolean + `selection_basis` Utf8,
# #2059/#2060; TAGS only at `top_k=None`, `_EXPECTED_RUN_ID` byte-identical).
# So NEITHER parent literal reproduces on the folded tree (train-b's
# `b0a3bed9…1bae` lacked the exec-grid run_ids; the member's `7671178b…02ec`
# lacked the additive columns) — RE-DERIVED FRESH in the Train-B integrity pass
# (the house law: goldens are re-derived, never merged textually; two-run
# byte-determinism + persisted>0 pinned below, polars/zstd-build sensitive per
# the module docstring).
_EXPECTED_STORE_HASH = "e9dd823a17f215f5db9e55d176c739e8226aa5f231e0bf050f42c541d8f37bc0"


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
        # The stitched slice IS the real 1-minute stream (interval "1minute",
        # tf_minutes == 1) — the §6 fixed point: it is passed as BOTH the
        # decision and the execution feed (DEC-FILL-EXEC-GRID-1M-001).
        return run_persisted_sweep(
            bars, cfg, store=store, dedup=dedup, audit=audit, exec_bars_1m=bars
        )


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
