# pyright: reportPrivateUsage=none
"""L2 post-pull heal wiring at the dashboard pull seams (DEC-LAKE-HEAL-001).

Pins the pull-seam glue contract on BOTH L2 wirings:

* ``brutex.dashboard.historical_pull._heal_lake_after_pull`` — the F&O twin,
  called by ``pull_contract_candles`` inside ``if not dry_run:`` BEFORE the
  Greeks enrichment (the seam's downstream 1-minute reader now that the
  mid-pull daily derive is removed — DEC-PIPELINE-AUTOPREBUILD-001): a
  pre-poisoned month inside the pulled span is healed end-to-end
  (raw-pyarrow dup → keep-FIRST winner on disk + restorable quarantine
  copy), the heal runs BEFORE ``_enrich_greeks_from_minute`` (order spy), a
  dry-run never heals, ``LakeWriteHalt`` from the heal PROPAGATES out of the
  pull (disk-full = pull halt), and every other heal error is swallowed +
  logged ``lake_heal.post_pull_error`` (a heal bug never fails a pull).
  Target derivation is pinned: no-window / unresolvable-exchange contracts
  contribute nothing; the span is the UNION of the resolved windows.

* ``brutex.dashboard.pull_controller._heal_after_pull`` — the spot glue
  (``# pragma: no cover`` raw-lake-I/O-glue convention, so its contract is
  pinned HERE): heals a poisoned pulled slice, re-raises ``LakeWriteHalt``,
  swallows + logs everything else.

Offline throughout: fake Groww SDK (canned candles), a recording sink for the
ingest writes, a REAL temp registry + lake; poison planted with raw pyarrow
(the #1067 convention — the fixed writer refuses duplicates).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.partition import build_partition_path
from brutex.data.sink import SinkAppendResult
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

import brutex.data.lake_heal as lake_heal_module
from brutex.adapters.groww.client import GrowwClient
from brutex.dashboard import historical_pull as hp
from brutex.dashboard import pull_controller as pc
from brutex.data.registry_store import RegistryStore

from tests.fakes.lake_poison import poison_partition as _poison_partition
from tests.fakes.lake_poison import stored_timestamps as _stored_timestamps


@pytest.fixture(autouse=True)
def _endgame_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the ONE (Rust) heal/scan arm — W-6a deleted the Python cores."""
    monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)


_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_RUN_ID = "heal-seam-test"
_FETCHED = datetime(2026, 1, 1, tzinfo=UTC)
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)
_CE_SYMBOL = "NSE-NIFTY-25Jan24-21000-CE"


# ── offline fakes (the test_pull_historical shapes) ──────────────────────────


class _FakeSDK:
    """A ``GrowwSDK``-shaped fake returning one canned candle per fetch."""

    def get_historical_candles(self, **_kw: object) -> dict[str, object]:
        return {"candles": [["2024-01-15T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        """Protocol completeness only — the candle pull never resolves expiries."""
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        """Protocol completeness only — the candle pull never resolves contracts."""
        return {"contracts": []}


class _RecordingSink:
    """A minimal recording ``BarSink`` (the ingest writes stay off the lake)."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, str, int]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[object],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.appends.append((segment, symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _factory(root_unused: Path) -> tuple[GrowwClient, Any]:
    return GrowwClient(sdk=_FakeSDK(), sleep=lambda _s: None, max_attempts=1), _RecordingSink()


@pytest.fixture
def registry(tmp_path: Path) -> RegistryStore:
    """A real temp registry seeded with one NIFTY CE contract."""
    store = RegistryStore.open(tmp_path / "registry.duckdb")
    store.upsert_contract(
        "NSE",
        "NIFTY",
        date(2024, 1, 25),
        _CE_SYMBOL,
        side="CE",
        strike=21000.0,
        run_id=_RUN_ID,
        fetched_at=_FETCHED,
    )
    return store


@pytest.fixture
def tmp_lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


def _fno_ts(minute: int) -> datetime:
    """A UTC instant inside the January-2024 partition (Wed 3 Jan, IST session)."""
    return datetime(2024, 1, 3, 4, minute, tzinfo=UTC)


def _seed_option_tree_foreign(lake: Path, *, home_ts: datetime, foreign_ts: datetime) -> Path:
    """One REAL-writer options partition raw-rewritten to carry a foreign row.

    Seeds through :class:`OptionParquetWriter` (the #1067 convention: clean
    data via the real writer, poison via a raw re-render — the writer routes
    by month so it can never store a misplaced row itself). Returns the
    poisoned month file (named for *home_ts*'s UTC month).
    """
    import polars as pl
    from brutex.data.models import OptionBar
    from brutex.data.option_parquet_writer import (
        OptionParquetWriter,
        OptionSeriesCoordinate,
    )

    price = Decimal("105.00")
    writer = OptionParquetWriter(lake_root=lake)
    writer.write(
        partition_key="NSE_FNO|13|WEEK|0|ATM",
        coordinate=OptionSeriesCoordinate(
            exch_id="NSE",
            exchange_segment="NSE_FNO",
            security_id=13,
            underlying_symbol="NIFTY",
            expiry_flag="WEEK",
            expiry_code=0,
            strike_offset="ATM",
        ),
        bars=[
            OptionBar(
                timestamp=home_ts,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=10,
                open_interest=100,
                side="CE",
                strike=Decimal("21000.00"),
                spot=None,
                iv=None,
            )
        ],
    )
    (dest,) = (lake / "options").rglob("*.parquet")
    df = pl.read_parquet(dest)
    stray = df[0:1].with_columns(pl.lit(foreign_ts).dt.replace_time_zone("UTC").alias("timestamp"))
    pl.concat([df, stray]).write_parquet(dest)
    return dest


def _poisoned_fno_dest(lake: Path) -> Path:
    dest = build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="FNO",
        symbol=_CE_SYMBOL,
        interval="1minute",
        year=2024,
        month=1,
    )
    _poison_partition(
        dest,
        [
            (_fno_ts(16), 100.0, 10),  # keep-first winner
            (_fno_ts(16), 999.0, 99),  # the poisoned duplicate
            (_fno_ts(17), 101.0, 11),
        ],
    )
    return dest


def _pull(registry: RegistryStore, tmp_lake: Path, *, dry_run: bool = False) -> object:
    return hp.pull_contract_candles(
        registry.list_contracts(underlying="NIFTY"),
        lake_root=tmp_lake,
        dry_run=dry_run,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=_factory,
        now=_NOW,
    )


def _contract(symbol: str = _CE_SYMBOL) -> Any:
    from brutex.adapters.protocol import DerivativeContract

    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2024, 1, 25),
        symbol=symbol,
        contract_type="CE",
        strike=21000.0,
    )


# ── F&O seam: pull_contract_candles ──────────────────────────────────────────


class TestFnoPullSeam:
    def test_live_pull_heals_poisoned_month_in_span(
        self, registry: RegistryStore, tmp_lake: Path
    ) -> None:
        """The seam pin: a poisoned month inside the pulled span is healed."""
        dest = _poisoned_fno_dest(tmp_lake)
        original_bytes = dest.read_bytes()

        _pull(registry, tmp_lake, dry_run=False)

        assert _stored_timestamps(dest) == [_fno_ts(16), _fno_ts(17)]
        quarantined = list((tmp_lake / ".quarantine").rglob("01.parquet"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == original_bytes

    def test_heal_runs_before_greeks_enrichment(
        self, registry: RegistryStore, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order pin: the heal fires BEFORE the Greeks enrichment reads the 1-minute.

        (The mid-pull daily derive was removed by DEC-PIPELINE-AUTOPREBUILD-001;
        the downstream 1-minute reader at this seam is now the Greeks enrichment,
        and the pipeline's final prebuild phase runs strictly later.)
        """
        order: list[str] = []
        monkeypatch.setattr(hp, "_heal_lake_after_pull", lambda **_kw: order.append("heal"))

        def _fake_greeks(**_kw: object) -> hp._GreeksEnrichOutcome:
            # Honour the DEC-GREEKS-ENRICH-OBSERVE-001 return contract — the
            # seam reads ``greeks_outcome.ok`` for the report + note, so the
            # order spy must return a real outcome, never ``None``.
            order.append("greeks")
            return hp._GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)

        monkeypatch.setattr(hp, "_enrich_greeks_from_minute", _fake_greeks)
        _pull(registry, tmp_lake, dry_run=False)
        assert order[:2] == ["heal", "greeks"]

    def test_dry_run_never_heals(
        self, registry: RegistryStore, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []
        monkeypatch.setattr(hp, "_heal_lake_after_pull", lambda **_kw: called.append("heal"))
        _pull(registry, tmp_lake, dry_run=True)
        assert called == []

    def test_lake_write_halt_from_heal_propagates(
        self, registry: RegistryStore, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disk-full heal halts the pull exactly like any lake write."""

        def _halt(**_kw: object) -> object:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(lake_heal_module, "heal_after_pull", _halt)
        with pytest.raises(LakeWriteHalt):
            _pull(registry, tmp_lake, dry_run=False)

    def test_heal_error_swallowed_and_logged(
        self, registry: RegistryStore, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A heal bug never fails a successful pull — swallowed + WARNING."""

        def _boom(**_kw: object) -> object:
            raise RuntimeError("heal bug")

        monkeypatch.setattr(lake_heal_module, "heal_after_pull", _boom)
        with capture_logs() as logs:
            _pull(registry, tmp_lake, dry_run=False)
        events = [e for e in logs if e["event"] == "lake_heal.post_pull_error"]
        assert len(events) == 1
        assert "heal bug" in str(events[0]["error"])

    def test_fno_pull_heals_options_tree_foreign(
        self, registry: RegistryStore, tmp_lake: Path
    ) -> None:
        """DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 seam pin: a foreign row in an
        in-pull-span ``options/`` month file is reassigned home by the REAL
        pull, source cleaned, quarantine copy kept, event tree-tagged."""
        import polars as pl

        dest = _seed_option_tree_foreign(
            tmp_lake,
            home_ts=_fno_ts(16),
            foreign_ts=datetime(2023, 12, 15, 4, 16, tzinfo=UTC),
        )
        original_bytes = dest.read_bytes()

        with capture_logs() as logs:
            _pull(registry, tmp_lake, dry_run=False)

        stored = pl.read_parquet(dest)
        assert stored["timestamp"].to_list() == [_fno_ts(16)]  # source cleaned
        moved_home = dest.parent / "2023-12.parquet"
        assert moved_home.exists()
        assert pl.read_parquet(moved_home)["timestamp"].to_list() == [
            datetime(2023, 12, 15, 4, 16, tzinfo=UTC)
        ]
        quarantined = list((tmp_lake / ".quarantine").rglob("2024-01.parquet"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == original_bytes
        events = [
            e
            for e in logs
            if e["event"] == "lake_heal.rows_reassigned" and e.get("tree") == "options"
        ]
        assert len(events) == 1
        assert events[0]["moved"] == 1

    def test_options_scan_month_bounded(self, registry: RegistryStore, tmp_lake: Path) -> None:
        """An options poison OUTSIDE the pulled span is left byte-untouched
        (bytes + mtime pinned) — the L2 months bound covers options too.
        Honest scope of THESE assertions: bytes+mtime prove never WRITTEN;
        the stronger "clean out-of-scope file is never even read" claim
        rides the pure-FILENAME months bound and is pinned by the census
        call-shape spy + the read-spy scanner unit tests, not here."""
        dest = _seed_option_tree_foreign(
            tmp_lake,
            home_ts=datetime(2022, 6, 15, 4, 16, tzinfo=UTC),
            foreign_ts=datetime(2022, 7, 1, 4, 16, tzinfo=UTC),
        )
        before_bytes = dest.read_bytes()
        before_mtime = dest.stat().st_mtime_ns

        _pull(registry, tmp_lake, dry_run=False)

        assert dest.read_bytes() == before_bytes
        assert dest.stat().st_mtime_ns == before_mtime

    def test_dry_run_leaves_options_poison_byte_untouched(
        self, registry: RegistryStore, tmp_lake: Path
    ) -> None:
        """The dry-run pin extended to the options tree: not one byte moves."""
        dest = _seed_option_tree_foreign(
            tmp_lake,
            home_ts=_fno_ts(16),
            foreign_ts=datetime(2023, 12, 15, 4, 16, tzinfo=UTC),
        )
        before = dest.read_bytes()
        _pull(registry, tmp_lake, dry_run=True)
        assert dest.read_bytes() == before
        assert not (tmp_lake / ".quarantine").exists()


class TestFnoGlueTargets:
    """Direct pins on ``_heal_lake_after_pull``'s target/span derivation."""

    def test_no_window_and_bad_exchange_contribute_nothing(
        self, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(lake_heal_module, "heal_after_pull", lambda **kw: calls.append(kw))
        rebuilt = cast(
            Any,
            [
                (None, _contract()),  # resolvable, windowed → the ONE target
                (None, _contract("NSE-NIFTY-25Jan24-22000-CE")),  # no window → skipped
                (None, _contract("NOHYPHEN")),  # unresolvable exchange → skipped
            ],
        )
        windows = {
            _CE_SYMBOL: (date(2024, 1, 1), date(2024, 1, 20)),
            "NOHYPHEN": (date(2024, 2, 1), date(2024, 2, 10)),
        }
        hp._heal_lake_after_pull(rebuilt=rebuilt, windows=windows, lake_root=tmp_lake)
        assert len(calls) == 1
        assert calls[0]["targets"] == [("NSE", "FNO", _CE_SYMBOL)]
        assert calls[0]["start"] == date(2024, 1, 1)
        assert calls[0]["end"] == date(2024, 1, 20)

    def test_fno_seam_opts_into_the_options_census(
        self, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The F&O seam passes ``include_options=True`` — the ONE seam owning
        the options/ wiring (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001)."""
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(lake_heal_module, "heal_after_pull", lambda **kw: calls.append(kw))
        rebuilt = cast(Any, [(None, _contract())])
        windows = {_CE_SYMBOL: (date(2024, 1, 1), date(2024, 1, 20))}
        hp._heal_lake_after_pull(rebuilt=rebuilt, windows=windows, lake_root=tmp_lake)
        assert len(calls) == 1
        assert calls[0]["include_options"] is True

    def test_span_is_union_of_windows(
        self, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(lake_heal_module, "heal_after_pull", lambda **kw: calls.append(kw))
        other = "NSE-NIFTY-25Jan24-21000-PE"
        rebuilt = cast(Any, [(None, _contract()), (None, _contract(other))])
        windows = {
            _CE_SYMBOL: (date(2024, 1, 5), date(2024, 1, 20)),
            other: (date(2023, 12, 1), date(2024, 1, 10)),
        }
        hp._heal_lake_after_pull(rebuilt=rebuilt, windows=windows, lake_root=tmp_lake)
        assert calls[0]["start"] == date(2023, 12, 1)
        assert calls[0]["end"] == date(2024, 1, 20)

    def test_zero_targets_is_a_no_op(self, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(lake_heal_module, "heal_after_pull", lambda **kw: calls.append(kw))
        hp._heal_lake_after_pull(rebuilt=cast(Any, []), windows={}, lake_root=tmp_lake)
        assert calls == []


# ── spot seam: pull_controller._heal_after_pull glue ─────────────────────────


def _instrument(symbol: str = "NIFTY") -> Any:
    return SimpleNamespace(
        exch_id=SimpleNamespace(value="NSE"),
        segment=SimpleNamespace(value="INDEX"),
        symbol_name=symbol,
    )


def _spot_ts(minute: int) -> datetime:
    """A UTC instant inside the June-2024 partition (Mon 3 Jun, IST session)."""
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


class TestSpotGlue:
    def test_glue_heals_poisoned_pulled_slice(self, tmp_lake: Path) -> None:
        dest = build_partition_path(
            lake_root=tmp_lake,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            year=2024,
            month=6,
        )
        _poison_partition(
            dest,
            [(_spot_ts(16), 100.0, 10), (_spot_ts(16), 999.0, 99), (_spot_ts(17), 101.0, 11)],
        )
        original_bytes = dest.read_bytes()

        pc._heal_after_pull(
            instruments=[_instrument()],
            lake_root=tmp_lake,
            start=date(2024, 6, 1),
            end=date(2024, 6, 30),
            pull_id="pid-1",
        )

        assert _stored_timestamps(dest) == [_spot_ts(16), _spot_ts(17)]
        quarantined = list((tmp_lake / ".quarantine").rglob("06.parquet"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == original_bytes

    def test_lake_write_halt_re_raises(
        self, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _halt(**_kw: object) -> object:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(lake_heal_module, "heal_after_pull", _halt)
        with pytest.raises(LakeWriteHalt):
            pc._heal_after_pull(
                instruments=[_instrument()],
                lake_root=tmp_lake,
                start=date(2024, 6, 1),
                end=date(2024, 6, 30),
                pull_id="pid-2",
            )

    def test_other_errors_swallowed_and_logged(
        self, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(**_kw: object) -> object:
            raise RuntimeError("heal bug")

        monkeypatch.setattr(lake_heal_module, "heal_after_pull", _boom)
        with capture_logs() as logs:
            pc._heal_after_pull(
                instruments=[_instrument()],
                lake_root=tmp_lake,
                start=date(2024, 6, 1),
                end=date(2024, 6, 30),
                pull_id="pid-3",
            )
        events = [e for e in logs if e["event"] == "lake_heal.post_pull_error"]
        assert len(events) == 1
        assert events[0]["pull_id"] == "pid-3"

    def test_spot_glue_leaves_options_poison_untouched(self, tmp_lake: Path) -> None:
        """The spot seam keeps the default ``include_options=False`` — ONE
        seam (the F&O glue) owns the options wiring
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001)."""
        dest = _seed_option_tree_foreign(
            tmp_lake,
            home_ts=datetime(2024, 6, 3, 4, 16, tzinfo=UTC),
            foreign_ts=datetime(2024, 7, 1, 4, 16, tzinfo=UTC),
        )
        before = dest.read_bytes()
        pc._heal_after_pull(
            instruments=[_instrument()],
            lake_root=tmp_lake,
            start=date(2024, 6, 1),
            end=date(2024, 6, 30),
            pull_id="pid-4",
        )
        assert dest.read_bytes() == before  # byte-untouched — spot never opts in


# ── L2 catalog rotation (fix-pass: the seams open <lake>/catalog.duckdb) ─────


def _catalog_row(catalog_path: Path, **coords: Any) -> Any:
    from brutex.data.catalog import Catalog

    with Catalog.open(catalog_path) as cat:
        return cat.get(**coords)


class TestL2CatalogRotation:
    """The L2 seams mirror the L3 sweeper's catalog convention (fix-pass).

    When ``<lake>/catalog.duckdb`` EXISTS, both glue functions open it
    best-effort (:func:`brutex.data.lake_heal.open_lake_catalog`) and pass it
    to ``heal_after_pull`` — so a healed partition's rotated ``raw_hash``
    reaches the catalog IMMEDIATELY, not at the next L1 reconcile. Fails
    before the seams were wired (they passed ``catalog=None`` and the stale
    pre-heal hash survived the heal).
    """

    def test_fno_glue_rotates_the_healed_partitions_catalog_row(self, tmp_lake: Path) -> None:
        import hashlib

        from brutex.data.catalog import Catalog
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        dest = _poisoned_fno_dest(tmp_lake)
        catalog_path = tmp_lake / "catalog.duckdb"
        coords = {
            "exchange": "NSE",
            "segment": "FNO",
            "symbol": _CE_SYMBOL,
            "interval": "1minute",
            "year": 2024,
            "month": 1,
        }
        with Catalog.open(catalog_path) as cat:  # register the POISONED bytes' hash
            reconcile_catalog_from_lake(lake_root=tmp_lake, catalog=cat, check_invariants=False)
        before = _catalog_row(catalog_path, **coords)
        assert before is not None
        assert before.raw_hash == hashlib.sha256(dest.read_bytes()).hexdigest()

        rebuilt = cast(Any, [(None, _contract())])
        windows = {_CE_SYMBOL: (date(2024, 1, 1), date(2024, 1, 20))}
        hp._heal_lake_after_pull(rebuilt=rebuilt, windows=windows, lake_root=tmp_lake)

        assert _stored_timestamps(dest) == [_fno_ts(16), _fno_ts(17)]  # healed
        after = _catalog_row(catalog_path, **coords)
        assert after is not None
        assert after.raw_hash != before.raw_hash  # rotated AT the L2 seam
        assert after.raw_hash == hashlib.sha256(dest.read_bytes()).hexdigest()

    def test_spot_glue_rotates_the_healed_partitions_catalog_row(self, tmp_lake: Path) -> None:
        import hashlib

        from brutex.data.catalog import Catalog
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        dest = build_partition_path(
            lake_root=tmp_lake,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            year=2024,
            month=6,
        )
        _poison_partition(
            dest,
            [(_spot_ts(16), 100.0, 10), (_spot_ts(16), 999.0, 99), (_spot_ts(17), 101.0, 11)],
        )
        catalog_path = tmp_lake / "catalog.duckdb"
        coords = {
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "1minute",
            "year": 2024,
            "month": 6,
        }
        with Catalog.open(catalog_path) as cat:
            reconcile_catalog_from_lake(lake_root=tmp_lake, catalog=cat, check_invariants=False)
        before = _catalog_row(catalog_path, **coords)
        assert before is not None

        pc._heal_after_pull(
            instruments=[_instrument()],
            lake_root=tmp_lake,
            start=date(2024, 6, 1),
            end=date(2024, 6, 30),
            pull_id="pid-cat",
        )

        assert _stored_timestamps(dest) == [_spot_ts(16), _spot_ts(17)]  # healed
        after = _catalog_row(catalog_path, **coords)
        assert after is not None
        assert after.raw_hash != before.raw_hash
        assert after.raw_hash == hashlib.sha256(dest.read_bytes()).hexdigest()
