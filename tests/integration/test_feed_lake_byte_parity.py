"""Cross-cutting proof suite for the feed-root nesting law (the parity harness).

DEC-FEED-LAKE-ROOT-001 (MIGRATION.md #1753) / DEC-FEED-INTERFACE-001 (#1752).
The sacred invariant of the feed-add program is that the DEFAULT feed's write
output + every existing lake reader's behaviour are byte-identical whether or not
the feed-root machinery exists, and a non-default feed's nested tree
(``<lake_root>/feeds/<feed_id>/...``) is INVISIBLE to every existing lake
reader/scanner. This module proves all four legs the task requires:

1. **Whole-tree byte-parity** — the default feed written through the sanctioned
   sink constructor (:func:`brutex.feeds.binding.build_feed_sink`) produces
   parquet bytes byte-identical to the direct :class:`ParquetBarSink`, resolves
   to the TOP lake root by object identity, and creates neither a ``feeds/`` nor
   an ``.audit/`` dir; adding a non-default feed subtree in place leaves every
   top-root file byte-unchanged. The frozen engine golden
   (``_GOLDEN_CONTENT_HASH`` / ``_GOLDEN_RUN_ID``) reproduces with the feeds
   package imported — zero engine drift.
2. **Reader-invisibility** — every existing reader/scanner is byte/value-equal
   with and WITHOUT a populated ``feeds/feedx/`` tree present on the SAME lake
   (the honest ± methodology: add the subtree IN PLACE so the only variable is
   the feed tree, never two independently-written lakes with divergent mtimes).
   Each reader is a separate test so a future reader that descends into
   ``feeds/`` fails its own pin. Enumerated verdict table in the module docstring
   of :class:`TestReaderInvisibility`.
3. **Dedup-disjointness** — a cross-feed same-instant bar lands as TWO retained
   files (top-root + ``feeds/feedx/``); neither dedups the other and the O(1)
   keep-first ``timestamp`` dedup identity is unchanged.
4. **Engine negative** — the engine reads only the top-root catalog / lake, so a
   feed tree cannot reach a sweep; the frozen sweep goldens reproduce.

See TRACK1_BACKTEST_SPEC.md §"Data lake / partitioning" and ADR-0021.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from brutex.dashboard.db_view import (
    list_candle_datasets,
    list_fno_candle_datasets,
    list_option_datasets,
    summarize_spot_datasets,
)
from brutex.dashboard.lake_revision import lake_revision
from brutex.dashboard.movers_snapshot import compute_spot_candidates
from brutex.data.catalog import Catalog
from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
from brutex.data.feed_audit import FeedAuditSink
from brutex.data.feed_root import DEFAULT_FEED, feed_lake_root, feed_of_partition_path
from brutex.data.lake_heal import lake_content_token, run_startup_sweep
from brutex.data.lake_invariants import scan_lake_invariants
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import (
    OptionParquetWriter,
    OptionSeriesCoordinate,
)
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink, SinkAppendResult
from brutex.feeds.binding import build_feed_sink
from brutex.feeds.groww_binding import build_groww_binding

# The feeds package is imported at module import (above), so every assertion in
# this file runs with the feed-root machinery reachable — the "with the machinery
# present" arm of the byte-parity guarantee.

_IST = timezone(timedelta(hours=5, minutes=30))

_SPOT_EXCHANGE = "NSE"
_SPOT_SEGMENT = "INDEX"
_SPOT_SYMBOL = "NIFTY"
_FNO_SEGMENT = "FNO"
_FNO_SYMBOL = "NSE-NIFTY-30Jan25-FUT"
_INTERVAL = "1minute"
_OPTION_KEY = "NSE_FNO|25|MONTH|1|ATM-10|CALL"

#: A synthetic non-default feed id — grammar-valid, vendor-token-free (the
#: registry ships only the default feed; PR-2 stays no-vendor-guard-green).
_FEED_ID = "feedx"


def _spot_bars(*, base: float = 100.0) -> list[CandleBar]:
    """Six valid in-session NIFTY minute bars (stored UTC), closes drifting up."""
    out: list[CandleBar] = []
    for i in range(6):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal(str(round(base + i / 10, 2))),
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _option_bar(side: str, *, minute: int = 0) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2024, 3, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("48000.00"),
        spot=Decimal("47980.55"),
        iv=Decimal("0.1523"),
    )


def _write_spot(lake_root: Path, *, segment: str, symbol: str, base: float = 100.0) -> None:
    """Write one spot/FNO candle series into a lake root via the direct sink."""
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(lake_root / "catalog.duckdb") as cat:
        ParquetBarSink(writer=writer, catalog=cat).append(
            exchange=_SPOT_EXCHANGE,
            segment=segment,
            symbol=symbol,
            interval=_INTERVAL,
            bars=_spot_bars(base=base),
        )


def _write_option(lake_root: Path, *, minute: int = 0) -> None:
    """Write one CE+PE option pair into a lake root via the production writer."""
    seg, sec, flag, code, offset, _typ = _OPTION_KEY.split("|")
    OptionParquetWriter(lake_root=lake_root).write(
        partition_key=_OPTION_KEY,
        coordinate=OptionSeriesCoordinate(
            exch_id="NSE",
            exchange_segment=seg,
            security_id=int(sec),
            underlying_symbol="NIFTY",
            expiry_flag=flag,
            expiry_code=int(code),
            strike_offset=offset,
        ),
        bars=[_option_bar("CE", minute=minute), _option_bar("PE", minute=minute)],
    )


def _build_groww_lake(root: Path) -> None:
    """Populate *root* with a realistic default-feed lake (spot + FNO + options)."""
    root.mkdir(parents=True, exist_ok=True)
    _write_spot(root, segment=_SPOT_SEGMENT, symbol=_SPOT_SYMBOL)
    _write_spot(root, segment=_FNO_SEGMENT, symbol=_FNO_SYMBOL)
    _write_option(root)


def _add_feed_tree(root: Path, *, feed: str = _FEED_ID) -> Path:
    """Populate ``<root>/feeds/<feed>/`` with a nested spot + FNO + options tree.

    Written with DIFFERENT close values than the default lake so a cross-feed
    read that leaked would be caught by value, not just by count. Returns the
    resolved feed root.
    """
    feed_root = feed_lake_root(root, feed=feed)
    _write_spot(feed_root, segment=_SPOT_SEGMENT, symbol=_SPOT_SYMBOL, base=101.0)
    _write_spot(feed_root, segment=_FNO_SEGMENT, symbol=_FNO_SYMBOL, base=101.0)
    _write_option(feed_root)
    return feed_root


def _tree_sha_map(root: Path, *, subdirs: tuple[str, ...]) -> dict[str, str]:
    """A ``{rel_posix_path: sha256(content)}`` map over ``*.parquet`` in *subdirs*.

    Content-only (never mtime/size), so it captures byte-identity of the stored
    lake independent of when it was written.
    """
    out: dict[str, str] = {}
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.parquet")):
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _reader_snapshot(root: Path) -> dict[str, Any]:
    """Every existing lake reader's output for *root*, in a value-comparable shape."""
    snap: dict[str, Any] = {}
    snap["list_candle_datasets"] = list_candle_datasets(root)
    snap["list_fno_candle_datasets"] = list_fno_candle_datasets(root)
    snap["list_option_datasets"] = list_option_datasets(root)
    conn = duckdb.connect()
    try:
        snap["summarize_spot_datasets"] = summarize_spot_datasets(conn, root)
    finally:
        conn.close()
    scan = scan_lake_invariants(root)
    snap["scan_files_scanned"] = scan.files_scanned
    snap["scan_files_by_segment"] = dict(scan.files_by_segment)
    snap["scan_findings"] = len(scan.findings)
    snap["lake_content_token"] = lake_content_token(root)
    snap["lake_revision"] = lake_revision(root)
    with Catalog.open(root / "catalog.duckdb") as cat:
        rec = reconcile_catalog_from_lake(lake_root=root, catalog=cat)
    snap["reconcile_partitions"] = rec.partitions_reconciled
    snap["reconcile_bars_total"] = rec.bars_total
    snap["reconcile_findings"] = len(rec.findings)
    cand = compute_spot_candidates(
        lake_root=root,
        interval=_INTERVAL,
        date="2024-01-02",
        time=None,
        exchange=_SPOT_EXCHANGE,
    )
    snap["movers"] = sorted((s.instrument.symbol, str(s.ltp)) for s in cand.snapshots)
    return snap


# ─────────────────────────── whole-tree byte-parity ───────────────────────────


class TestWholeTreeByteParity:
    """The default feed writes byte-identically and a feed subtree is inert."""

    def test_default_feed_sink_is_plain_and_writes_top_root(self, tmp_path: Path) -> None:
        """``build_feed_sink`` for the default feed → a bare sink on the TOP root.

        No ``FeedAuditSink`` wrap, no ``feeds/`` dir, no ``.audit/`` chain — the
        byte-zero-new-code guarantee (DEC-FEED-LAKE-ROOT-001).
        """
        root = tmp_path / "lake"
        root.mkdir()
        binding = build_groww_binding()
        sink = build_feed_sink(lake_root=root, binding=binding)
        assert isinstance(sink, ParquetBarSink)
        assert not isinstance(sink, FeedAuditSink)
        # The sanctioned constructor resolves the default feed to the top root.
        assert binding.lake_root_for(root) is root
        result = sink.append(
            exchange=_SPOT_EXCHANGE,
            segment=_SPOT_SEGMENT,
            symbol=_SPOT_SYMBOL,
            interval=_INTERVAL,
            bars=_spot_bars(),
        )
        assert isinstance(result, SinkAppendResult)
        assert (root / "bars" / _SPOT_EXCHANGE / _SPOT_SEGMENT / _SPOT_SYMBOL).is_dir()
        assert not (root / "feeds").exists()
        assert not (root / ".audit").exists()

    def test_default_feed_sink_bytes_equal_direct_sink(self, tmp_path: Path) -> None:
        """The default feed's parquet bytes == the direct ``ParquetBarSink`` bytes."""
        direct = tmp_path / "direct"
        direct.mkdir()
        with Catalog.open(direct / "catalog.duckdb") as cat:
            ParquetBarSink(writer=ParquetWriter(lake_root=direct), catalog=cat).append(
                exchange=_SPOT_EXCHANGE,
                segment=_SPOT_SEGMENT,
                symbol=_SPOT_SYMBOL,
                interval=_INTERVAL,
                bars=_spot_bars(),
            )
        via_feed = tmp_path / "via_feed"
        via_feed.mkdir()
        build_feed_sink(lake_root=via_feed, binding=build_groww_binding()).append(
            exchange=_SPOT_EXCHANGE,
            segment=_SPOT_SEGMENT,
            symbol=_SPOT_SYMBOL,
            interval=_INTERVAL,
            bars=_spot_bars(),
        )
        direct_map = _tree_sha_map(direct, subdirs=("bars",))
        feed_map = _tree_sha_map(via_feed, subdirs=("bars",))
        assert direct_map, "structural non-vacuity: the direct lake has parquet files"
        assert feed_map == direct_map

    def test_top_tree_bytes_unchanged_by_feed_subtree(self, tmp_path: Path) -> None:
        """Adding a ``feeds/feedx`` tree in place leaves every top-root file intact."""
        root = tmp_path / "lake"
        _build_groww_lake(root)
        before = _tree_sha_map(root, subdirs=("bars", "options"))
        assert before, "structural non-vacuity: the default lake has parquet files"
        _add_feed_tree(root)
        after_top = _tree_sha_map(root, subdirs=("bars", "options"))
        assert after_top == before
        # ...and the feed subtree really is on disk (non-vacuous).
        feed_files = list((root / "feeds" / _FEED_ID).rglob("*.parquet"))
        assert feed_files, "the feed subtree must carry parquet files"


class TestEngineGoldenReproducesWithFeedsImported:
    """The frozen engine golden reproduces with the feeds package imported."""

    def test_run_id_and_content_hash_reproduce(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        # The engine kernel is a compiled Rust extension; skip cleanly where it
        # is not built (this env), run in CI where it is — the point is proving
        # zero engine drift with the feeds package imported, which is a CI fact.
        pytest.importorskip("fno_math")
        # Imported here (not at module top) to make the dependency explicit: the
        # golden helpers own the fixture; we only re-drive it with feeds present.
        from brutex.engine.sweep_run import mask_run_id, run_persisted_sweep

        from tests.golden.test_engine_sweep_run_golden import (
            _CTX,
            _GOLDEN_CONTENT_HASH,
            _GOLDEN_RUN_ID,
            _PARAMS,
            _bars,
            _config,
        )

        # feed_root is reachable in this process; the run identity must not move.
        assert feed_of_partition_path(tmp_path, lake_root=tmp_path) == DEFAULT_FEED
        assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) == _GOLDEN_RUN_ID

        from brutex.data.option_dedup import OptionDedupIndex
        from brutex.engine.results_store import ResultsStore
        from brutex.engine.sweep_run import ComboDedup
        from brutex.orchestrator.audit import AuditWriter

        store = ResultsStore(path=tmp_path / "s.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter(path=tmp_path / "s.jsonl") as audit:
            summary = run_persisted_sweep(_bars(), _config(), store=store, dedup=dedup, audit=audit)
        assert summary.store_content_hash == _GOLDEN_CONTENT_HASH


# ─────────────────────────── reader-invisibility ───────────────────────────


@pytest.fixture(scope="module")
def invisibility_snapshots(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Build one lake, snapshot every reader ± a feed tree added IN PLACE.

    Module-scoped so the (moderately expensive) lake build + reader sweep runs
    ONCE for all parametrized reader assertions. Returns
    ``{"before": {...}, "after": {...}, "root": str}``.
    """
    root = tmp_path_factory.mktemp("invis") / "lake"
    _build_groww_lake(root)
    # Prime the discovery + stats manifests (a real lake serves manifest-first
    # after its first walk) so the ± comparison covers the manifest-served path.
    list_candle_datasets(root)
    summarize_spot_datasets(duckdb.connect(), root)
    before = _reader_snapshot(root)
    feed_root = _add_feed_tree(root)
    # Prime the FEED tree's own manifests too — proves a fully-initialised nested
    # tree (its own .manifest/, catalog, bars, options) stays inert.
    list_candle_datasets(feed_root)
    after = _reader_snapshot(root)
    return {"before": before, "after": after, "root": str(root)}


class TestReaderInvisibility:
    """Every existing lake reader is byte/value-equal with vs without a feed tree.

    Verdict table (reader → verdict; every one INVISIBLE-ALREADY — the feed
    subtree ``feeds/<f>/`` is a SIBLING of every anchor dir a reader walks
    (``bars/`` / ``options/`` / ``.manifest/`` / ``sweeps/`` / catalog), so no
    reader ever descends into it; ZERO reader source edits were required):

    ==============================================  ==================
    reader                                          verdict
    ==============================================  ==================
    db_view.list_candle_datasets (+ manifest-first) invisible-already
    db_view.list_fno_candle_datasets                invisible-already
    db_view.list_option_datasets                    invisible-already
    db_view.summarize_spot_datasets (completeness)  invisible-already
    movers_snapshot.compute_spot_candidates         invisible-already
    catalog_reconcile.reconcile_catalog_from_lake   invisible-already
    lake_invariants.scan_lake_invariants (census)   invisible-already
    lake_heal.lake_content_token                    invisible-already
    lake_heal.run_startup_sweep (healer L3)         invisible-already
    lake_manifest (top-root discovery manifest)     invisible-already
    lake_stats_manifest (top-root stats manifest)   invisible-already
    lake_revision (composite cache token)           invisible-already
    ==============================================  ==================

    Methodology: ONE lake, snapshot each reader, add the feed tree IN PLACE,
    snapshot again — the only variable is the feed subtree (never two
    independently-written lakes, whose stat-token readers would legitimately
    differ by mtime). Each reader is asserted separately so a regression names
    exactly which reader started seeing ``feeds/``.
    """

    def test_structural_non_vacuity(self, invisibility_snapshots: dict[str, Any]) -> None:
        root = Path(invisibility_snapshots["root"])
        assert list((root / "feeds" / _FEED_ID / "bars").rglob("*.parquet"))
        assert list((root / "feeds" / _FEED_ID / "options").rglob("*.parquet"))
        # The feed tree built its own manifest/catalog — a fully-realised sibling.
        assert (root / "feeds" / _FEED_ID / ".manifest").exists()
        assert (root / "feeds" / _FEED_ID / "catalog.duckdb").exists()

    @pytest.mark.parametrize(
        "reader",
        [
            "list_candle_datasets",
            "list_fno_candle_datasets",
            "list_option_datasets",
            "summarize_spot_datasets",
            "scan_files_scanned",
            "scan_files_by_segment",
            "scan_findings",
            "lake_content_token",
            "lake_revision",
            "reconcile_partitions",
            "reconcile_bars_total",
            "reconcile_findings",
            "movers",
        ],
    )
    def test_reader_invisible(self, invisibility_snapshots: dict[str, Any], reader: str) -> None:
        before = invisibility_snapshots["before"][reader]
        after = invisibility_snapshots["after"][reader]
        assert after == before, (
            f"{reader} changed after adding a feeds/{_FEED_ID} subtree — the feed "
            f"tree is NOT invisible to it (before={before!r}, after={after!r})"
        )

    def test_healer_startup_sweep_invisible(self, tmp_path: Path) -> None:
        """The L3 heal sweep (scan + token sentinel) never touches ``feeds/``."""
        root = tmp_path / "lake"
        _build_groww_lake(root)
        before = run_startup_sweep(root, per_file_pause=lambda: None)
        _add_feed_tree(root)
        # The token must be UNCHANGED, so the second sweep token-matches (no-op).
        after = run_startup_sweep(root, per_file_pause=lambda: None)
        assert before.token == after.token
        assert after.ran is False  # sentinel matched → the feed tree is inert to L3


# ─────────────────────────── dedup-disjointness ───────────────────────────


class TestDedupDisjointness:
    """A cross-feed same-instant bar → two retained files, neither dedups the other."""

    def test_same_instant_bars_land_in_two_files(self, tmp_path: Path) -> None:
        root = tmp_path / "lake"
        root.mkdir()
        # SAME (exchange, segment, symbol, interval, minute) — different close values.
        with Catalog.open(root / "catalog.duckdb") as cat:
            top = ParquetBarSink(writer=ParquetWriter(lake_root=root), catalog=cat).append(
                exchange=_SPOT_EXCHANGE,
                segment=_SPOT_SEGMENT,
                symbol=_SPOT_SYMBOL,
                interval=_INTERVAL,
                bars=_spot_bars(base=100.0),
            )
        feed = build_feed_sink(lake_root=root, binding=_synthetic_feed_binding()).append(
            exchange=_SPOT_EXCHANGE,
            segment=_SPOT_SEGMENT,
            symbol=_SPOT_SYMBOL,
            interval=_INTERVAL,
            bars=_spot_bars(base=101.0),
        )
        # Two distinct files, both fully retained (keep-first dedup identity
        # unchanged: the timestamp key is scoped to a single partition FILE).
        top_file = (
            root
            / "bars"
            / _SPOT_EXCHANGE
            / _SPOT_SEGMENT
            / _SPOT_SYMBOL
            / _INTERVAL
            / "2024"
            / "01.parquet"
        )
        feed_file = (
            root
            / "feeds"
            / _FEED_ID
            / "bars"
            / _SPOT_EXCHANGE
            / _SPOT_SEGMENT
            / _SPOT_SYMBOL
            / _INTERVAL
            / "2024"
            / "01.parquet"
        )
        assert top_file.is_file()
        assert feed_file.is_file()
        assert top_file.read_bytes() != feed_file.read_bytes()  # distinct values kept
        # Both writes report every bar as genuinely new — neither dedups the other.
        assert top.bars_net_new == 6
        assert feed.bars_net_new == 6
        assert sum(len(wr.conflicts) for wr in top.write_results) == 0
        assert sum(len(wr.conflicts) for wr in feed.write_results) == 0


def _synthetic_feed_binding() -> Any:
    """A minimal grammar-valid non-default binding for the dedup/audit tests.

    Vendor-token-free (feed id ``feedx``); reuses the default feed's harmless
    control-plane callables/constants since only ``feed_id`` + ``lake_root_for``
    matter for the write path exercised here.
    """
    from functools import partial

    from brutex.feeds.binding import FeedBinding

    groww = build_groww_binding()
    return FeedBinding(
        feed_id=_FEED_ID,
        ledger_market=_FEED_ID,
        ledger_market_future=f"{_FEED_ID}-future",
        ledger_market_option=f"{_FEED_ID}-option",
        adapter_factory=groww.adapter_factory,
        window_days_for=groww.window_days_for,
        error_policy=groww.error_policy,
        pacing=groww.pacing,
        client_self_paces=groww.client_self_paces,
        client_self_reports_rate_limits=groww.client_self_reports_rate_limits,
        client_self_reports_successes=groww.client_self_reports_successes,
        history_floor=None,
        lake_root_for=partial(feed_lake_root, feed=_FEED_ID),
    )
